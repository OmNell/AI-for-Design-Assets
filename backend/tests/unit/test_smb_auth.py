from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from smbprotocol.connection import Connection
from smbprotocol.exceptions import SMBAuthenticationError, SMBOSError, SMBConnectionClosed, SMBException
from smbprotocol.header import Commands, NtStatus, SMB2HeaderRequest, SMB2HeaderResponse
from smbprotocol.session import SMB2SessionSetupResponse, SessionFlags

from app.config import Settings, PathsConfig
from app.services.crypto import SecretCipher
from app.services.nas import NasAlreadyExists, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, SmbFs
from app.services.nas.errors import NasLoginError, translate
from app.services.nas.factory import create_system_fs
from app.services.nas.smb import DirectConnection, direct_session


@pytest.fixture
def smb_mock(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    def connect(host: str, share: str, username: str, password: str, timeout: float) -> SimpleNamespace:
        assert password == "dummy-secret"
        calls.append({"host": host, "share": share, "username": username})
        connection = SimpleNamespace(transport=SimpleNamespace(connected=True), disconnect=Mock())
        return SimpleNamespace(session=SimpleNamespace(password=None, connection=connection), is_dfs_share=False)
    monkeypatch.setattr("app.services.nas.smb.direct_session", connect)
    monkeypatch.setattr("app.services.nas.smb.smbclient.lstat", Mock(return_value=SimpleNamespace(st_file_attributes=0)))
    return calls


def test_smb_private_connections_disconnect_requires_new_login(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = SmbFs("nas", "share", "alice", "dummy-secret"), SmbFs("nas", "share", "alice", "dummy-secret")
    assert a._cache is not b._cache
    assert a._session.connection is not b._session.connection
    assert a._session.password is None and "_password" not in vars(a)
    listing = Mock(side_effect=[SMBConnectionClosed("断线"), ["file"]])
    monkeypatch.setattr("app.services.nas.smb.smbclient.listdir", listing)
    with pytest.raises(NasSessionExpired):
        a.list("工作台临时区")
    with pytest.raises(NasSessionExpired):
        a.list("工作台临时区")
    assert listing.call_count == 1 and len(smb_mock) == 2
    assert b.list("工作台临时区") == ["file"]
    assert listing.call_args_list[0].kwargs["connection_cache"] is a._cache
    assert listing.call_args_list[1].kwargs["connection_cache"] is b._cache
    a.close()
    b.close()
    assert not a._cache and a._session is None


def test_smb_mutations_never_reconnect_or_replay_after_disconnect(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    fs._session.connection.transport.connected = False
    mkdir = Mock()
    monkeypatch.setattr("app.services.nas.smb.smbclient.mkdir", mkdir)
    with pytest.raises(NasSessionExpired):
        fs.mkdir("工作台临时区/folder")
    assert len(smb_mock) == 1 and not mkdir.called
    fs.close()
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    rename = Mock(side_effect=ConnectionResetError("写请求结果不确定 dummy-secret"))
    monkeypatch.setattr("app.services.nas.smb.smbclient.rename", rename)
    with pytest.raises(NasSessionExpired) as error:
        fs.rename("工作台临时区/a", "工作台临时区/b")
    assert rename.call_count == 1 and len(smb_mock) == 2
    assert "dummy-secret" not in str(error.value)
    fs.close()


def test_smb_permission_does_not_reconnect_and_reparse_denied(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    monkeypatch.setattr("app.services.nas.smb.smbclient.listdir", Mock(side_effect=SMBOSError(0xC0000022, "目录")))
    with pytest.raises(NasPermissionDenied):
        fs.list("工作台临时区")
    assert len(smb_mock) == 1
    monkeypatch.setattr("app.services.nas.smb.smbclient.lstat", Mock(return_value=SimpleNamespace(st_file_attributes=0x400)))
    with pytest.raises(NasPermissionDenied):
        fs.open_write("工作台临时区/linked/file")
    fs.close()


def test_smb_stream_failure_not_replayed_and_blocks_reconnect(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    raw = Mock()
    raw.write.side_effect = SMBConnectionClosed("追加中断")
    opening = Mock(return_value=raw)
    monkeypatch.setattr("app.services.nas.smb.smbclient.open_file", opening)
    with fs.open_write("工作台临时区/file", append=True) as handle:
        with pytest.raises(NasSessionExpired):
            handle.write(b"payload")
        fs._session.connection.transport.connected = False
        with pytest.raises(NasSessionExpired, match="重新登录"):
            fs.list("工作台临时区")
    assert raw.write.call_count == 1 and opening.call_count == 1
    assert len(smb_mock) == 1
    fs.close()


@pytest.mark.parametrize("error,expected", [(SMBOSError(0xC0000022, "x"), NasPermissionDenied),
    (SMBOSError(0xC0000034, "x"), NasNotFound), (SMBOSError(0xC0000035, "x"), NasAlreadyExists),
    (ConnectionError("x"), NasUnavailable), (SMBOSError(0xC0000043, "x"), NasUnavailable), (OSError(errno.ENOTEMPTY, "x"), NasUnavailable)])
def test_exception_translation(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(translate(error), expected)


@pytest.fixture
def smb_stages(monkeypatch: pytest.MonkeyPatch):
    events = SimpleNamespace(negotiate=Mock(), authenticate=Mock(), tree=Mock(), disconnect=Mock())
    sessions = []
    connections = []

    def negotiate(connection, timeout):
        assert connection.require_signing
        connection.transport = SimpleNamespace(connected=True)
        connections.append(connection)
        events.negotiate()

    class SmbSession:
        def __init__(self, connection, username, password, require_encryption, auth_protocol):
            self.connection = connection
            self.username, self.password = username, password
            self.session_id, self.session_key = 1, b"simulated-session-key"
            self.signing_required, self.encrypt_data = True, False
            assert auth_protocol == "ntlm"
            sessions.append(self)

        def connect(self):
            assert self.username == "alice" and self.password == "dummy-secret"
            events.authenticate()

    class SmbTree:
        is_dfs_share = False

        def __init__(self, session, name):
            self.session = session
            assert name == r"\\nas\share"
            assert session.password is None

        def connect(self):
            events.tree()

    monkeypatch.setattr(DirectConnection, "connect", negotiate)
    monkeypatch.setattr(DirectConnection, "disconnect", events.disconnect)
    monkeypatch.setattr("app.services.nas.smb.Session", SmbSession)
    monkeypatch.setattr("app.services.nas.smb.TreeConnect", SmbTree)
    return events, sessions, connections


def test_smb_accepts_a_signed_session_only_after_share_connect(smb_stages) -> None:
    events, sessions, connections = smb_stages
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    assert fs._session is sessions[0] and fs._cache.get("nas:445") is connections[0]
    assert fs.browser_tree.session is sessions[0] and connections[0].bound_to_share
    assert sessions[0].password is None
    events.negotiate.assert_called_once()
    events.authenticate.assert_called_once()
    events.tree.assert_called_once()
    fs.close()
    events.disconnect.assert_called_once()


@pytest.mark.parametrize(("phase", "failure", "code", "status_code"), [
    ("negotiate", OSError(errno.ECONNREFUSED, "dummy-secret"), "smb_unavailable", 503),
    ("negotiate", OSError(errno.EHOSTUNREACH, "dummy-secret"), "nas_network_unreachable", 503),
    ("negotiate", SMBException("dummy-secret"), "smb_negotiation_failed", 503),
    ("authenticate", SMBOSError(NtStatus.STATUS_LOGON_FAILURE, "dummy-secret"), "invalid_credentials", 401),
    ("authenticate", SMBOSError(0xC0000072, "dummy-secret"), "nas_account_restricted", 403),
    ("authenticate", SMBAuthenticationError("dummy-secret"), "smb_auth_failed", 503),
    ("tree", SMBOSError(NtStatus.STATUS_ACCESS_DENIED, "dummy-secret"), "nas_share_permission_denied", 403),
    ("tree", SMBOSError(NtStatus.STATUS_BAD_NETWORK_NAME, "dummy-secret"), "nas_share_not_found", 503),
])
def test_smb_login_failure_is_classified_and_connection_closed(smb_stages, phase, failure, code, status_code) -> None:
    events, sessions, _ = smb_stages
    getattr(events, phase).side_effect = failure
    with pytest.raises(NasLoginError) as caught:
        direct_session("nas", "share", "alice", "dummy-secret", 5)
    error = caught.value
    assert (error.code, error.status_code) == (code, status_code)
    assert "dummy-secret" not in str(error) and error.__suppress_context__
    events.disconnect.assert_called_once_with(close=False, timeout=5)
    assert all(session.password is None for session in sessions)
    if phase != "tree":
        events.tree.assert_not_called()


@pytest.mark.parametrize("flag", [SessionFlags.SMB2_SESSION_FLAG_IS_GUEST, SessionFlags.SMB2_SESSION_FLAG_IS_NULL])
def test_guest_and_anonymous_setup_response_rejected(monkeypatch: pytest.MonkeyPatch, flag: int) -> None:
    setup = SMB2SessionSetupResponse()
    setup["session_flags"] = flag
    message = SMB2HeaderRequest()
    message["command"] = Commands.SMB2_SESSION_SETUP
    response = SMB2HeaderResponse()
    response["status"] = NtStatus.STATUS_SUCCESS
    response["data"] = setup.pack()
    monkeypatch.setattr(Connection, "receive", Mock(return_value=response))
    connection = DirectConnection("nas", 5)
    with pytest.raises(NasLoginError) as caught:
        connection.receive(SimpleNamespace(message=message))
    assert caught.value.code == "nas_guest_rejected" and caught.value.status_code == 401


def test_cached_smb_connection_blocks_reauthentication_and_other_servers(smb_stages) -> None:
    _, _, connections = smb_stages
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    with pytest.raises(NasPermissionDenied):
        fs._cache.get("other:445")
    with pytest.raises(NasPermissionDenied):
        connections[0].send(SimpleNamespace(COMMAND=Commands.SMB2_TREE_CONNECT))
    with pytest.raises(NasSessionExpired):
        connections[0].send(SimpleNamespace(COMMAND=Commands.SMB2_SESSION_SETUP))
    fs.close()


def test_system_factory_decrypts_only_system_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Settings(mode="prod", paths=PathsConfig(data_dir=tmp_path))
    config.nas.system_password_enc = SecretStr(SecretCipher(tmp_path).encrypt("dummy-secret"))
    constructor = Mock(return_value="系统会话替身")
    monkeypatch.setattr("app.services.nas.factory.SmbFs", constructor)
    assert create_system_fs(config) == "系统会话替身"
    assert constructor.call_args.args[2:4] == ("wb-system", "dummy-secret")
    permissions = constructor.call_args.args[-1]
    assert permissions == {"formal": "read", "temp": "none", "backup": "write"}
