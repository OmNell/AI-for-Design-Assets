"""所有测试用本地替身，禁止依赖真实 NAS 或其他外部系统。"""
from __future__ import annotations

import errno
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from unittest.mock import Mock

import httpx
import pytest
from smbprotocol.exceptions import SMBOSError

SPEC = importlib.util.spec_from_file_location("probe_nas", Path(__file__).parents[1] / "probe_nas.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class LocalFs:
    def __init__(self, root: Path, deny: bool = False) -> None:
        self.root = root
        self.deny = deny
        self.append_count = 0
        self.created: list[str] = []

    def local(self, path: str) -> Path:
        return self.root.joinpath(*path.split("\\"))

    def list(self, path: str) -> list[str]:
        return [p.name for p in self.local(path).iterdir()]

    def mkdir(self, path: str) -> None:
        self.local(path).mkdir()
        self.created.append(path)

    def open(self, path: str, mode: str) -> BinaryIO:
        if self.deny and path.startswith("只读\\") and mode != "rb":
            raise SMBOSError(0xC0000022, path)
        if mode == "ab":
            self.append_count += 1
        return self.local(path).open(mode)

    def rename(self, source: str, target: str) -> None:
        self.local(source).rename(self.local(target))

    def delete(self, path: str, directory: bool = False) -> None:
        target = self.local(path)
        target.rmdir() if directory else target.unlink()


@pytest.fixture
def local(tmp_path: Path) -> LocalFs:
    (tmp_path / "读写").mkdir()
    (tmp_path / "只读").mkdir()
    return LocalFs(tmp_path)


def test_file_checks_append_hash_rename_paths_and_cleanup(local: LocalFs) -> None:
    untouched = local.root / "读写" / "原有.txt"
    untouched.write_text("保留", encoding="utf-8")
    recorder = probe.Recorder([])
    probe.file_checks(local, "读写", recorder, "A")
    assert all(r.status == "成功" for r in recorder.results)
    assert local.append_count == 23
    assert any(p.endswith("05 详情页") for p in local.created)
    assert any(p.endswith("p" * 200) for p in local.created)
    assert list((local.root / "读写").iterdir()) == [untouched]


def test_partial_write_failure_cleans_only_owned_objects(local: LocalFs, monkeypatch: pytest.MonkeyPatch) -> None:
    original = local.open
    def broken(path: str, mode: str) -> BinaryIO:
        if mode == "ab" and path.endswith("payload.bin"):
            raise OSError("模拟网络中断")
        return original(path, mode)
    monkeypatch.setattr(local, "open", broken)
    recorder = probe.Recorder([])
    probe.file_checks(local, "读写", recorder, "A")
    assert any(r.step == "追加写入 20MiB" and r.status == "失败" for r in recorder.results)
    assert any(r.step == "同卷 rename" and r.status == "跳过" for r in recorder.results)
    assert not list((local.root / "读写").iterdir())


def test_mkdir_denied_skips_dependents(local: LocalFs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "mkdir", Mock(side_effect=PermissionError("拒绝")))
    recorder = probe.Recorder([])
    probe.file_checks(local, "读写", recorder, "A")
    assert recorder.results[1].status == "失败"
    assert all(row.status == "跳过" for row in recorder.results[2:])


def test_hash_mismatch_and_slow_rename_cleanup(local: LocalFs, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = probe.Workspace(local)
    workspace.mkdir("读写\\test")
    workspace.write("读写\\test\\file", 1)
    with pytest.raises(ValueError, match="读回不一致"):
        workspace.verify("读写\\test\\file", "错误摘要", probe.CHUNK_SIZE)
    ticks = iter([0.0, 1.5])
    with monkeypatch.context() as patch:
        patch.setattr(probe.time, "perf_counter", lambda: next(ticks))
        with pytest.raises(ValueError, match="未达到"):
            workspace.rename("读写\\test", "读写\\moved")
    assert workspace.owned[0][0] == "读写\\moved"
    workspace.cleanup(probe.Recorder([]), "A")
    assert not list((local.root / "读写").iterdir())


def test_permission_denial_type_and_code(local: LocalFs) -> None:
    local.deny = True
    detail = probe.permission_check(local, "只读", True)
    assert "0xC0000022" in detail and "SMBOSError" in detail
    local.deny = False
    with pytest.raises(ValueError, match="实际允许"):
        probe.permission_check(local, "只读", True)
    assert not list((local.root / "只读").iterdir())
    assert "允许写入" in probe.permission_check(local, "只读", False)
    assert not list((local.root / "只读").iterdir())


def test_missing_directory_and_transport_error_are_not_acl_pass(local: LocalFs, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FileNotFoundError):
        probe.permission_check(local, "不存在", True)
    monkeypatch.setattr(local, "open", Mock(side_effect=ConnectionError("断线")))
    with pytest.raises(ConnectionError):
        probe.permission_check(local, "只读", True)
    assert not probe.is_denied(SMBOSError(0xC0000043, "共享冲突"))
    assert probe.is_denied(PermissionError(errno.EACCES, "拒绝"))


@pytest.mark.parametrize("path", ["../x", "a/../b", "/tmp", "\\\\host\\share", "C:\\x", "a//b", "a.", "a ", "a\nb"])
def test_invalid_paths(path: str) -> None:
    with pytest.raises(probe.argparse.ArgumentTypeError):
        probe.relative_path(path)


def test_dsm_discovery_post_login_logout_and_redaction() -> None:
    requests: list[httpx.Request] = []
    recorder = probe.Recorder(["p&a ss"])
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        if "SYNO.API.Info" in body:
            payload = {"success": True, "data": {"SYNO.API.Auth": {
                "path": "entry.cgi", "minVersion": 3, "maxVersion": 7}}}
        elif "method=login" in body:
            payload = {"success": True, "data": {"sid": "secret-sid"}}
        else:
            payload = {"success": True}
        return httpx.Response(200, json=payload)
    dsm = probe.DsmClient("nas", 5001, True, True, 10, recorder.secrets.append)
    dsm.client.close()
    dsm.client = httpx.Client(base_url="https://nas:5001/webapi/", transport=httpx.MockTransport(handle))
    assert "v6" in dsm.login("alice", "p&a ss")
    assert dsm.sid == "secret-sid"
    assert "p%26a+ss" in requests[1].content.decode()
    assert all(r.method == "POST" and not r.url.query for r in requests)
    assert "secret-sid" not in recorder.redact("secret-sid p&a ss p%26a+ss")
    assert "p&a ss" not in recorder.redact("secret-sid p&a ss p%26a+ss")
    dsm.logout()
    assert dsm.sid is None and dsm.client.is_closed
    assert "method=logout" in requests[-1].content.decode()


def test_dsm_failure_preserves_error_and_no_smb_attempt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe.DsmClient, "login", Mock(side_effect=probe.DsmError({"success": False, "error": {"code": 403}})))
    connect = Mock()
    monkeypatch.setattr(probe.SmbNasFs, "connect", connect)
    assert probe.main(args()) == 1
    connect.assert_not_called()
    output = capsys.readouterr().out
    assert '"code": 403' in output and "汇总表" in output
    assert "pw-a" not in output and "pw-b" not in output


def test_smb_adapter_uses_private_caches_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[SimpleNamespace] = []
    calls: list[dict[str, object]] = []
    def register(host: str, **kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        session = SimpleNamespace(password=kwargs["password"], connection=SimpleNamespace(transport=SimpleNamespace(connected=True)))
        instances.append(session)
        return session
    monkeypatch.setattr(probe.smbclient, "register_session", register)
    for name in ("listdir", "mkdir", "rename", "remove", "rmdir", "reset_connection_cache"):
        monkeypatch.setattr(probe.smbclient, name, Mock(return_value=[]))
    monkeypatch.setattr(probe.smbclient, "open_file", Mock(return_value=io.BytesIO()))
    a = probe.SmbNasFs("nas", "共享", "alice", 10)
    b = probe.SmbNasFs("nas", "共享", "bob", 10)
    for fs in (a, b):
        fs.connect("secret")
        fs.list("目录")
        fs.mkdir("目录\\x")
        fs.open("目录\\x\\f", "xb")
        fs.rename("目录\\x", "目录\\y")
        fs.delete("目录\\y\\f")
        fs.delete("目录\\y", True)
    assert a.cache is not b.cache
    assert calls[0]["connection_cache"] is a.cache
    assert calls[1]["connection_cache"] is b.cache
    assert all(s.password is None for s in instances)
    assert "原会话未替换" in probe.check_coexistence([a, b], "目录")
    for name in ("listdir", "mkdir", "open_file", "rename", "remove", "rmdir"):
        method = getattr(probe.smbclient, name)
        for call in method.call_args_list:
            options = call.kwargs
            assert options["connection_cache"] is (a.cache if options["username"] == "alice" else b.cache)
            assert "password" not in options
    a.close()
    assert probe.smbclient.reset_connection_cache.call_args.kwargs["connection_cache"] is a.cache


def args() -> list[str]:
    return ["--host", "nas", "--share", "共享", "--test-dir", "读写", "--read-only-dir", "只读",
            "--user", "alice", "--password", "pw-a", "--user", "bob", "--password", "pw-b"]


def test_two_account_full_flow(local: LocalFs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    adapters: list[LocalFs] = []
    class FakeSmb(LocalFs):
        def __init__(self, host: str, share: str, username: str, timeout: float) -> None:
            super().__init__(local.root, deny=username == "alice")
            self.cache: dict[str, object] = {}
            self.session = None
            adapters.append(self)
        def connect(self, password: str) -> str:
            assert password == ("pw-a" if self.deny else "pw-b")
            self.session = SimpleNamespace(connection=object())
            return "已连接"
        def close(self) -> str:
            self.session = None
            return "已关闭"
    monkeypatch.setattr(probe, "SmbNasFs", FakeSmb)
    monkeypatch.setattr(probe.DsmClient, "login", lambda *a: "成功")
    assert probe.main(args()) == 0
    output = capsys.readouterr().out
    assert "双账号同时存活：成功" in output and "B 加入后仍拒写：成功" in output
    assert "pw-a" not in output and "pw-b" not in output
    assert all(fs.session is None for fs in adapters)
    assert not list((local.root / "读写").iterdir())
    assert not list((local.root / "只读").iterdir())


def test_connection_failure_closes_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe.DsmClient, "login", lambda *a: "成功")
    monkeypatch.setattr(probe.SmbNasFs, "connect", Mock(side_effect=ConnectionError("断开 pw-a")))
    assert probe.main(args()) == 1
    output = capsys.readouterr().out
    assert "关闭 SMB：成功" in output and "双账号同时存活：跳过" in output
    assert "pw-a" not in output


def test_cli_bad_arguments_never_echo_password(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        probe.main(args() + ["--mistyped-password", "SUPER_SECRET"])
    assert error.value.code == 2
    assert "SUPER_SECRET" not in capsys.readouterr().err


def test_cleanup_failure_is_visible(local: LocalFs, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = probe.Workspace(local)
    workspace.mkdir("读写\\test")
    monkeypatch.setattr(local, "delete", Mock(side_effect=SMBOSError(0xC0000022, "读写\\test")))
    recorder = probe.Recorder([])
    workspace.cleanup(recorder, "A")
    assert recorder.results[0].status == "失败"
    assert "读写\\test" in recorder.results[0].detail


def test_hidden_password_input_cannot_fall_back_to_echo(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import warnings
    def unsafe(prompt: str) -> str:
        warnings.warn("无法隐藏", probe.getpass.GetPassWarning)
        raise AssertionError("不应继续读取")
    monkeypatch.setattr(probe.getpass, "getpass", unsafe)
    arguments = args()
    del arguments[-2:]
    first_password = arguments.index("--password")
    del arguments[first_password:first_password+2]
    assert probe.main(arguments) == 130
    assert "无法隐藏输入" in capsys.readouterr().err


def test_single_account_is_incomplete(local: LocalFs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe.DsmClient, "login", lambda *a: "成功")
    monkeypatch.setattr(probe.SmbNasFs, "connect", Mock(side_effect=ConnectionError("离线替身")))
    assert probe.main(args()[:-4]) == 1
    assert "未提供两个账号" in capsys.readouterr().out
