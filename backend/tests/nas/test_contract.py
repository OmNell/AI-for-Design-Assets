"""这组用例不区分实现；smb 参数统一携带 nas 标记，默认跳过。"""
from __future__ import annotations

import pytest

from app.services.nas import NasAlreadyExists, NasNotFound, NasPermissionDenied, NasUnavailable, Region
from .conftest import NasCase


def test_methods_append_offset_rename_and_delete(nas_case: NasCase) -> None:
    case = nas_case
    fs = case.first
    directory = case.temp("中文路径/05 详情页")
    fs.mkdir(directory, recursive=True)
    path = directory + "/验证.bin"
    assert fs.exists(path) is False
    with fs.open_write(path) as stream:
        assert stream.write(b"abc") == 3
    with fs.open_write(path, append=True) as stream:
        stream.write(b"def")
        stream.flush()
    assert fs.list(directory) == ["验证.bin"]
    result = fs.stat(path)
    assert result.size == 6 and not result.is_dir and result.mtime > 0
    assert fs.stat(directory).is_dir
    with fs.open_read(path, offset=2) as stream:
        assert stream.read(2) == b"cd"
        assert stream.read() == b"ef"
    with fs.open_read(path, offset=100) as stream:
        assert stream.read() == b""
    moved = case.temp("移动后")
    fs.rename(directory, moved)
    assert not fs.exists(path)
    with fs.open_read(moved + "/验证.bin") as stream:
        assert stream.read() == b"abcdef"
    fs.delete(moved + "/验证.bin")
    fs.delete(moved)
    assert not fs.exists(moved)
    fs.delete(case.temp("中文路径"))


def test_overwrite_nonrecursive_delete_and_errors(nas_case: NasCase) -> None:
    case = nas_case
    fs = case.first
    fs.mkdir(case.temp("folder"))
    with pytest.raises(NasAlreadyExists):
        fs.mkdir(case.temp("folder"))
    path = case.temp("folder/file")
    with fs.open_write(path) as stream:
        stream.write(b"original")
    with fs.open_write(path) as stream:
        stream.write(b"new")
    assert fs.stat(path).size == 3
    with pytest.raises(NasUnavailable):
        fs.delete(case.temp("folder"))
    with pytest.raises(NasNotFound):
        fs.stat(case.temp("missing"))
    with pytest.raises(NasNotFound):
        fs.open_read(case.temp("missing"))
    with pytest.raises(NasNotFound):
        fs.delete(case.temp("missing"))
    fs.delete(case.temp("folder"), recursive=True)
    with pytest.raises(ValueError):
        fs.open_read(path, offset=-1)


def test_account_acl_enforced_and_connections_coexist(nas_case: NasCase) -> None:
    case = nas_case
    a, b = case.first, case.second
    protected = case.formal("只读.txt")
    with b.open_write(protected) as stream:
        stream.write(b"readonly")
    with a.open_read(protected) as stream:
        assert stream.read() == b"readonly"
    assert a.exists(protected)
    assert "只读.txt" in a.list(case.paths.join(Region.FORMAL, case.scope))
    for operation in (lambda: a.open_write(protected), lambda: a.open_write(protected, append=True),
                      lambda: a.mkdir(case.formal("越权")), lambda: a.delete(protected),
                      lambda: a.rename(protected, case.temp("stolen"))):
        with pytest.raises(NasPermissionDenied):
            operation()
    source = case.temp("upload.bin")
    with a.open_write(source) as stream:
        stream.write(b"upload")
    with pytest.raises(NasPermissionDenied):
        a.rename(source, case.formal("upload.bin"))
    assert a.exists(source)
    # 终审本人移动，普通账号仍不可写；不允许用 B 连接代替 A。
    b.rename(source, case.formal("upload.bin"))
    with pytest.raises(NasPermissionDenied):
        a.open_write(case.formal("upload.bin"))
    assert a.list(case.paths.join(Region.TEMP, case.scope)) == []


def test_rename_never_overwrites_target(nas_case: NasCase) -> None:
    case = nas_case
    for name in ("one", "two"):
        with case.first.open_write(case.temp(name)) as stream:
            stream.write(name.encode())
    with pytest.raises(NasAlreadyExists):
        case.first.rename(case.temp("one"), case.temp("two"))
    with case.first.open_read(case.temp("two")) as stream:
        assert stream.read() == b"two"


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "C:\\data", "\\\\server\\share", "工作台临时区/../设计资产/x", "a//b", "a:b", "a\x00b", "a?b"])
def test_paths_cannot_escape(nas_case: NasCase, bad: str) -> None:
    fs = nas_case.first
    for operation in (lambda: fs.list(bad), lambda: fs.stat(bad), lambda: fs.exists(bad),
                      lambda: fs.open_read(bad), lambda: fs.open_write(bad), lambda: fs.mkdir(bad),
                      lambda: fs.rename(nas_case.temp("unused"), bad), lambda: fs.delete(bad)):
        with pytest.raises(NasPermissionDenied):
            operation()
