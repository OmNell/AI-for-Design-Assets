from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from smbprotocol.exceptions import SMBOSError, SMBConnectionClosed

from app.config import NasConfig, Settings, PathsConfig
from app.services.auth.backends import AuthUnavailable, DsmAuthBackend
from app.services.crypto import SecretCipher
from app.services.nas import NasAlreadyExists, NasNotFound, NasPermissionDenied, NasUnavailable, SmbFs
from app.services.nas.errors import translate
from app.services.nas.factory import create_auth_backend, create_system_fs


@pytest.fixture
def smb_mock(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    def register(host: str, **options: object) -> SimpleNamespace:
        # 测试不保留明文密码，只核对本人的凭据被显式传入。
        assert options["password"] == "dummy-secret"
        calls.append({key: value for key, value in options.items() if key != "password"})
        session = SimpleNamespace(password="dummy-secret", connection=SimpleNamespace(transport=SimpleNamespace(connected=True)))
        options["connection_cache"][host + ":445"] = session.connection
        return session
    def reset(**options: object) -> None:
        options["connection_cache"].clear()
    monkeypatch.setattr("app.services.nas.smb.smbclient.register_session", register)
    monkeypatch.setattr("app.services.nas.smb.smbclient.reset_connection_cache", reset)
    monkeypatch.setattr("app.services.nas.smb.smbclient.lstat", Mock(return_value=SimpleNamespace(st_file_attributes=0)))
    return calls


def test_smb_private_connections_retry_once_and_clear_secret(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = SmbFs("nas", "share", "alice", "dummy-secret"), SmbFs("nas", "share", "bob", "dummy-secret")
    assert a._cache is not b._cache
    assert a._session.connection is not b._session.connection
    assert a._session.password is None
    listing = Mock(side_effect=[SMBConnectionClosed("断线"), ["file"]])
    monkeypatch.setattr("app.services.nas.smb.smbclient.listdir", listing)
    assert a.list("工作台临时区") == ["file"]
    assert listing.call_count == 2 and len(smb_mock) == 3
    assert all(call.kwargs["connection_cache"] is a._cache for call in listing.call_args_list)
    assert all(call.kwargs["username"] == "alice" for call in listing.call_args_list)
    listing.side_effect = SMBConnectionClosed("仍断线")
    listing.reset_mock()
    with pytest.raises(NasUnavailable):
        a.list("工作台临时区")
    assert listing.call_count == 2
    assert len(smb_mock) == 4
    a.close()
    b.close()
    assert a._password is None and not a._cache and a._session is None


def test_smb_dead_before_mutation_reconnects_but_uncertain_mutation_not_replayed(smb_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    fs = SmbFs("nas", "share", "alice", "dummy-secret")
    fs._session.connection.transport.connected = False
    mkdir = Mock()
    monkeypatch.setattr("app.services.nas.smb.smbclient.mkdir", mkdir)
    fs.mkdir("工作台临时区/folder")
    assert len(smb_mock) == 2 and mkdir.call_count == 1
    rename = Mock(side_effect=ConnectionResetError("写请求结果不确定 dummy-secret"))
    monkeypatch.setattr("app.services.nas.smb.smbclient.rename", rename)
    with pytest.raises(NasUnavailable) as error:
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
        with pytest.raises(NasUnavailable):
            handle.write(b"payload")
        fs._session.connection.transport.connected = False
        with pytest.raises(NasUnavailable, match="活动文件句柄"):
            fs.list("工作台临时区")
    assert raw.write.call_count == 1 and opening.call_count == 1
    assert len(smb_mock) == 1
    fs.close()


@pytest.mark.parametrize("error,expected", [(SMBOSError(0xC0000022, "x"), NasPermissionDenied),
    (SMBOSError(0xC0000034, "x"), NasNotFound), (SMBOSError(0xC0000035, "x"), NasAlreadyExists),
    (ConnectionError("x"), NasUnavailable), (SMBOSError(0xC0000043, "x"), NasUnavailable), (OSError(errno.ENOTEMPTY, "x"), NasUnavailable)])
def test_exception_translation(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(translate(error), expected)


def dsm_transport(requests: list[httpx.Request], login_ok: bool = True, logout_ok: bool = True) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = parse_qs(request.content.decode())
        assert request.method == "POST" and not request.url.query
        if body["api"] == ["SYNO.API.Info"]:
            payload = {"success": True, "data": {"SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 3, "maxVersion": 7}}}
        elif body["method"] == ["login"]:
            assert body["account"] == ["alice"] and body["passwd"] == ["dummy-secret"]
            payload = {"success": True, "data": {"sid": "dummy-sid"}} if login_ok else {"success": False, "error": {"code": 400}}
        else:
            assert body["_sid"] == ["dummy-sid"]
            payload = {"success": logout_ok}
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handle)


def test_dsm_login_immediate_logout_no_token_retained() -> None:
    requests: list[httpx.Request] = []
    backend = DsmAuthBackend(NasConfig(), dsm_transport(requests))
    assert backend.verify("alice", "dummy-secret")
    assert len(requests) == 3
    assert parse_qs(requests[-1].content.decode())["method"] == ["logout"]
    assert "dummy-secret" not in repr(vars(backend)) and "dummy-sid" not in repr(vars(backend))


def test_dsm_rejected_identity_false_no_logout() -> None:
    requests: list[httpx.Request] = []
    backend = DsmAuthBackend(NasConfig(), dsm_transport(requests, login_ok=False))
    assert not backend.verify("alice", "dummy-secret")
    assert len(requests) == 2


def test_dsm_logout_failure_and_network_failure_sanitized() -> None:
    requests: list[httpx.Request] = []
    backend = DsmAuthBackend(NasConfig(), dsm_transport(requests, logout_ok=False))
    with pytest.raises(AuthUnavailable, match="注销失败"):
        backend.verify("alice", "dummy-secret")
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dummy-secret dummy-sid", request=request)
    backend = DsmAuthBackend(NasConfig(), httpx.MockTransport(broken))
    with pytest.raises(AuthUnavailable) as error:
        backend.verify("alice", "dummy-secret")
    assert "dummy-secret" not in str(error.value) and error.value.__suppress_context__


def test_dsm_discovery_cannot_redirect_credentials() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"SYNO.API.Auth": {
            "path": "//other-server/auth.cgi", "minVersion": 3, "maxVersion": 7}}})
    backend = DsmAuthBackend(NasConfig(), httpx.MockTransport(handle))
    with pytest.raises(AuthUnavailable, match="路径无效"):
        backend.verify("alice", "dummy-secret")


def test_system_factory_decrypts_only_system_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Settings(mode="prod", paths=PathsConfig(data_dir=tmp_path))
    config.nas.system_password_enc = SecretStr(SecretCipher(tmp_path).encrypt("dummy-secret"))
    constructor = Mock(return_value="系统会话替身")
    monkeypatch.setattr("app.services.nas.factory.SmbFs", constructor)
    assert isinstance(create_auth_backend(config), DsmAuthBackend)
    assert create_system_fs(config) == "系统会话替身"
    assert constructor.call_args.args[2:4] == ("wb-system", "dummy-secret")
    permissions = constructor.call_args.args[-1]
    assert permissions == {"formal": "read", "temp": "none", "backup": "write"}
