from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import Settings, DevUser, PathsConfig, load_settings
from app.services.nas import Access, LocalFs, NasPaths, NasPermissionDenied, NasSessionExpired, NasUnavailable, Region, SessionPool, safe_join
from app.services.nas.factory import create_auth_backend, create_fs, create_paths, create_session_pool, create_system_fs
from app.services.nas.paths import SYSTEM_PERMISSIONS


@pytest.mark.parametrize("path", ["a.", "a ", "CON.txt", "AUX", "a|b", "a<b", "a>b", 'a"b', "a*b", "a\nb", "", ".", "a/./b"])
def test_path_rejects_ambiguous_names(path: str) -> None:
    with pytest.raises(NasPermissionDenied):
        safe_join(path)


def test_paths_configured_roots_and_nonoverlap() -> None:
    paths = NasPaths("/assets/formal", "/assets/temp", "/assets/backup")
    assert paths.join(Region.TEMP, "中文/05 详情页") == "assets/temp/中文/05 详情页"
    with pytest.raises(ValueError):
        NasPaths("a", "a/b", "c")
    with pytest.raises(NasPermissionDenied):
        safe_join()


def test_local_no_access_and_system_permissions(tmp_path: Path) -> None:
    fs = LocalFs(tmp_path, SYSTEM_PERMISSIONS)
    for operation in (lambda: fs.exists("工作台临时区/missing"), lambda: fs.list("工作台临时区"),
                      lambda: fs.open_write("设计资产/file"), lambda: fs.delete("_工作台备份", True),
                      lambda: fs.rename("设计资产", "_工作台备份/formal")):
        with pytest.raises(NasPermissionDenied):
            operation()
    fs.mkdir("_工作台备份/folder")
    with fs.open_write("_工作台备份/folder/file") as stream:
        stream.write(b"backup")
    fs.delete("_工作台备份/folder", True)
    fs.close()
    with pytest.raises(NasUnavailable):
        fs.list("设计资产")


def test_symlink_is_rejected_even_if_target_is_inside(tmp_path: Path) -> None:
    fs = LocalFs(tmp_path, {Region.TEMP: Access.WRITE})
    target = tmp_path / "工作台临时区" / "real"
    target.mkdir()
    link = target.parent / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前测试账号不能创建符号链接")
    for operation in (lambda: fs.list("工作台临时区/link"), lambda: fs.open_write("工作台临时区/link/file"),
                      lambda: fs.stat("工作台临时区/link"), lambda: fs.delete("工作台临时区/link", True)):
        with pytest.raises(NasPermissionDenied):
            operation()
    assert target.is_dir()
    fs.close()


def test_session_pool_get_open_replace_close_concurrent(tmp_path: Path) -> None:
    def factory(username: str, password: str) -> LocalFs:
        return LocalFs(tmp_path, {Region.TEMP: Access.WRITE})
    with SessionPool(factory, lambda: LocalFs(tmp_path, SYSTEM_PERMISSIONS)) as pool:
        with pytest.raises(NasSessionExpired):
            pool.get(1)
        first = pool.open(1, "user", "dummy")
        assert pool.get(1) is first
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: pool.get(1), range(100)))
        assert all(result is first for result in results)
        second = pool.open(1, "user", "dummy")
        with pytest.raises(NasUnavailable):
            first.list("工作台临时区")
        assert pool.get(1) is second
        pool.close(1)
        pool.close(1)
        with pytest.raises(NasSessionExpired):
            pool.get(1)
        assert pool.system() is pool.system()
    with pytest.raises(NasSessionExpired):
        pool.get(1)


def test_pool_timeout_get_and_active_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("app.services.nas.fs.time.monotonic", lambda: clock[0])
    with SessionPool(lambda u, p: LocalFs(tmp_path, {Region.TEMP: Access.WRITE}),
                     lambda: LocalFs(tmp_path, SYSTEM_PERMISSIONS), idle_minutes=1) as pool:
        fs = pool.open(1, "user", "dummy")
        handle = fs.open_write("工作台临时区/open")
        clock[0] = 100
        pool.expire_idle()
        assert pool.get(1) is fs
        handle.write(b"active")
        handle.close()
        clock[0] = 161
        with pytest.raises(NasSessionExpired):
            pool.get(1)
        with pytest.raises(NasUnavailable):
            fs.stat("工作台临时区/open")
        old = pool.system()
        clock[0] = 222
        pool.expire_idle()
        assert pool.system() is not old


def test_sweeper_closes_without_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed = threading.Event()
    class ObservableFs(LocalFs):
        def _close_backend(self) -> None:
            closed.set()
    with SessionPool(lambda u, p: ObservableFs(tmp_path, {}), lambda: LocalFs(tmp_path, {}),
                     idle_minutes=0.0001, sweep_seconds=0.01) as pool:
        fs = pool.open(1, "user", "dummy")
        assert closed.wait(1), "后台回收线程未关闭空闲连接"
        with pytest.raises(NasSessionExpired):
            pool.get(1)


def test_factory_dev_config_and_real_system_not_used(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text('mode: dev\ndev_users:\n - username: demo\n   password: dummy\n   permissions: {formal: read, temp: write}\n', encoding="utf-8")
    config = load_settings(config_file)
    assert config.paths.dev_nas_dir == tmp_path / "devdata" / "nas"
    auth = create_auth_backend(config)
    assert auth.verify("demo", "dummy") and not auth.verify("demo", "wrong")
    assert not auth.verify("unknown", "dummy")
    assert "dummy" not in repr(config)
    with pytest.raises(NasPermissionDenied):
        create_fs(config, "demo", "wrong")
    with create_session_pool(config) as pool:
        fs = pool.open(1, "demo", "dummy")
        fs.mkdir(create_paths(config).join(Region.TEMP, "folder"))
        system = pool.system()
        with pytest.raises(NasPermissionDenied):
            system.list(config.nas.temp_path.removeprefix("/"))
        with pytest.raises(NasPermissionDenied):
            system.open_write("设计资产/bad")


def test_pool_failed_reauthentication_keeps_previous(tmp_path: Path) -> None:
    def factory(username: str, password: str) -> LocalFs:
        if password != "dummy":
            raise NasPermissionDenied("认证失败")
        return LocalFs(tmp_path, {})
    with SessionPool(factory, lambda: LocalFs(tmp_path, {})) as pool:
        old = pool.open(1, "user", "dummy")
        with pytest.raises(NasPermissionDenied):
            pool.open(1, "user", "wrong")
        assert pool.get(1) is old


def test_upload_pins_keep_connection_until_last_batch_and_allow_forced_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("app.services.nas.fs.time.monotonic", lambda: clock[0])
    with SessionPool(lambda u, p: LocalFs(tmp_path, {Region.TEMP: Access.WRITE}),
                     lambda: LocalFs(tmp_path, {}), idle_minutes=1) as pool:
        original = pool.open(1, "user", "dummy")
        assert pool.pin(1) is original
        assert pool.pin(1) is original
        clock[0] = 120
        pool.close(1)
        pool.expire_idle()
        assert pool.get(1) is original
        pool.unpin(1)
        assert pool.get(1) is original
        pool.unpin(1)
        with pytest.raises(NasSessionExpired):
            pool.get(1)
        replacement = pool.open(1, "user", "dummy")
        pool.pin(1)
        assert pool.open(1, "user", "dummy") is replacement
        pool.close(1, force=True)
        with pytest.raises(NasSessionExpired):
            pool.get(1)
