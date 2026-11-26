"""只读工作根访问，沿用当前设备的文件会话和句柄生命周期。"""
from __future__ import annotations

import io
import os
import re
import stat
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import BinaryIO, Iterator
from unicodedata import normalize

from smbprotocol.exceptions import NoMoreFiles, NoSuchFile
from smbprotocol.file_info import FileAttributes, FileInformationClass
from smbprotocol.open import CreateDisposition, CreateOptions, DirectoryAccessMask, ImpersonationLevel, Open, ShareAccess

from app.services.nas.errors import NasNotFound, NasPermissionDenied, NasUnavailable
from app.services.nas.fs import BaseFs, NasStat, NasStream
from app.services.nas.local import LocalFs
from app.services.nas.paths import Access, NasPaths, Region, safe_join, work_path
from app.services.nas.smb import DirectConnection, SmbFs


@dataclass(frozen=True)
class Entry:
    name: str
    stat: NasStat


def comparable(name: str) -> str:
    return normalize("NFKC", name).casefold()


def checked_relative(path: str) -> str:
    path = work_path(path, allow_share=True)
    if re.search(r"%[0-9a-f]{2}", path, re.I):
        raise NasPermissionDenied("路径包含未解析的编码")
    return path


class ReadRoot:
    def __init__(self, fs: BaseFs, root: str) -> None:
        self.fs = fs
        self.root = work_path(root)
        if fs.paths != NasPaths.under(self.root):
            raise NasPermissionDenied("NAS 工作根目录与当前会话不一致")
        if isinstance(fs, LocalFs):
            if not fs.browser_read:
                raise NasPermissionDenied("当前开发账号没有工作目录浏览权限")
        elif not isinstance(fs, SmbFs) or fs.browser_tree is None or fs.browser_tree.is_dfs_share:
            raise NasPermissionDenied("当前会话没有直连工作目录")

    def allowed(self, path: str) -> bool:
        name = comparable(path.partition("/")[0])
        if name in {comparable(self.fs.paths.root(r).rsplit("/", 1)[-1]) for r in (Region.TEMP, Region.BACKUP)}:
            return False
        if isinstance(self.fs, LocalFs) and name == comparable(self.fs.paths.formal.rsplit("/", 1)[-1]):
            return self.fs.permissions.get(Region.FORMAL, Access.NONE) != Access.NONE
        return True

    def checked(self, path: str) -> str:
        relative = checked_relative(path)
        if not self.allowed(relative):
            raise NasPermissionDenied("不允许访问此目录")
        work_path(safe_join(self.root, relative) if relative else self.root)
        return relative

    @contextmanager
    def local(self, fs: LocalFs, relative: str) -> Iterator[int | Path]:
        full = safe_join(self.root, relative) if relative else self.root
        if os.open not in os.supports_dir_fd:
            yield fs._path(full)
            return
        with ExitStack() as handles:
            top = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            parent = os.open(fs.root, top)
            handles.callback(os.close, parent)
            parts = full.split("/")
            for index, part in enumerate(parts):
                flags = top if index < len(parts) - 1 else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                # lstat 用于统一报告链接拒绝，O_NOFOLLOW 同时防止检查和打开之间的替换。
                metadata = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise NasPermissionDenied("不允许访问链接")
                if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                    raise NasNotFound("该路径不是目录")
                parent = os.open(part, flags, dir_fd=parent)
                handles.callback(os.close, parent)
            yield parent

    def list(self, path: str, folders_only: bool, cancelled: Callable[[], bool]) -> list[Entry]:
        relative = self.checked(path)
        return self.fs._run(lambda: self._list(relative, folders_only, cancelled))

    def _list(self, path: str, folders_only: bool, cancelled: Callable[[], bool]) -> list[Entry]:
        deadline = monotonic() + 10
        fs = self.fs
        if isinstance(fs, LocalFs):
            with self.local(fs, path) as target:
                if not stat.S_ISDIR(local_stat(target).st_mode):
                    raise NasNotFound("该路径不是目录")
                with os.scandir(target) as rows:
                    entries = collect(self, path, local_entries(rows, deadline), folders_only, cancelled)
        else:
            assert isinstance(fs, SmbFs)
            with smb_open(fs, self.root, path, directory=True, deadline=deadline) as handle:
                entries = collect(self, path, smb_entries(handle, deadline), folders_only, cancelled)
        return entries

    def info(self, path: str) -> NasStat:
        relative = self.checked(path)
        def read() -> NasStat:
            fs = self.fs
            if isinstance(fs, LocalFs):
                with self.local(fs, relative) as target:
                    return local_metadata(local_stat(target))
            assert isinstance(fs, SmbFs)
            with smb_open(fs, self.root, relative) as handle:
                return smb_metadata(handle.file_attributes, handle.end_of_file, handle.last_write_time)
        return self.fs._run(read)

    @contextmanager
    def open(self, path: str) -> Iterator[tuple[NasStat, BinaryIO]]:
        relative = self.checked(path)
        with ExitStack() as stack:
            fs = self.fs
            def acquire() -> tuple[NasStat, BinaryIO]:
                if isinstance(fs, LocalFs):
                    with self.local(fs, relative) as target:
                        meta = local_metadata(local_stat(target))
                        if meta.is_dir:
                            raise NasNotFound("该路径不是文件")
                        raw = os.fdopen(os.dup(target), "rb") if isinstance(target, int) else target.open("rb")
                else:
                    assert isinstance(fs, SmbFs)
                    handle = stack.enter_context(smb_open(fs, self.root, relative, directory=False))
                    meta = smb_metadata(handle.file_attributes, handle.end_of_file, handle.last_write_time)
                    raw = io.BufferedReader(SmbReader(handle, meta.size, stack.pop_all()))
                stream = NasStream(fs, raw)
                fs._streams.add(stream)
                # SMB 句柄也由流持有，关闭或退出时一起释放。
                return meta, stream
            meta, stream = fs._run(acquire)
            try:
                yield meta, stream
            finally:
                with fs._lock:
                    stream.close()


def local_stat(target: int | Path) -> os.stat_result:
    return os.fstat(target) if isinstance(target, int) else target.stat(follow_symlinks=False)


def local_metadata(value: os.stat_result) -> NasStat:
    if getattr(value, "st_file_attributes", 0) & 0x400 or not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
        raise NasPermissionDenied("不支持访问特殊文件")
    return NasStat(value.st_size, stat.S_ISDIR(value.st_mode), value.st_mtime)


def local_entries(rows: Iterator[os.DirEntry], deadline: float) -> Iterator[Entry]:
    for row in rows:
        check_deadline(deadline)
        try:
            value = row.stat(follow_symlinks=False)
            if not getattr(value, "st_file_attributes", 0) & 0x400:
                yield Entry(row.name, local_metadata(value))
        except (FileNotFoundError, PermissionError, NasPermissionDenied):
            continue


def collect(root: ReadRoot, path: str, rows: Iterator[Entry], folders_only: bool, cancelled: Callable[[], bool]) -> list[Entry]:
    entries = []
    for index, entry in enumerate(rows):
        if index % 64 == 0 and cancelled():
            raise NasUnavailable("目录读取已取消")
        if folders_only and not entry.stat.is_dir:
            continue
        try:
            full = safe_join(path, entry.name) if path else entry.name
            if "/" not in entry.name and "\\" not in entry.name and root.allowed(checked_relative(full)):
                work_path(safe_join(root.root, full))
                entries.append(entry)
        except NasPermissionDenied:
            continue
    return entries


def check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise NasUnavailable("NAS 目录读取超时，请重试")


READ_DIRECTORY = DirectoryAccessMask.FILE_LIST_DIRECTORY | DirectoryAccessMask.FILE_READ_ATTRIBUTES


@contextmanager
def smb_open(fs: SmbFs, root: str, relative: str, *, directory: bool | None = None,
             deadline: float | None = None) -> Iterator[Open]:
    fs._check_connection()
    tree = fs.browser_tree
    if tree is None or tree.is_dfs_share or not isinstance(tree.session.connection, DirectConnection):
        raise NasPermissionDenied("仅允许直连共享")
    connection = tree.session.connection
    original_timeout = connection.operation_timeout
    limit = monotonic() + 10 if deadline is None else deadline
    with ExitStack() as handles:
        handles.callback(setattr, connection, "operation_timeout", original_timeout)
        folders = root.split("/")
        parts = folders + (relative.split("/") if relative else [])
        parent: Open | None = None
        for index, part in enumerate(parts):
            check_deadline(limit)
            connection.operation_timeout = min(original_timeout, max(0.1, limit - monotonic()))
            last = index == len(parts) - 1
            if index >= len(folders):
                assert parent is not None
                # 只接受父目录列出的原始长名称，禁止通过 8.3 名称、别名访问其他目录。
                try:
                    matches = parent.query_directory(part, FileInformationClass.FILE_DIRECTORY_INFORMATION, max_output=4096)
                except (NoSuchFile, NoMoreFiles):
                    raise NasNotFound("NAS 文件或目录不存在") from None
                if not any(row["file_name"].get_value().decode("utf-16-le") == part for row in matches):
                    raise NasNotFound("NAS 文件或目录不存在")
            item = Open(tree, "\\".join(parts[:index + 1]))
            options = CreateOptions.FILE_OPEN_REPARSE_POINT
            if not last or directory is True:
                options |= CreateOptions.FILE_DIRECTORY_FILE
            elif directory is False:
                options |= CreateOptions.FILE_NON_DIRECTORY_FILE
            rights = READ_DIRECTORY if index >= len(folders) - 1 else DirectoryAccessMask.FILE_READ_ATTRIBUTES
            share = ShareAccess.FILE_SHARE_READ | (ShareAccess.FILE_SHARE_WRITE if not last or directory is True else 0)
            item.create(ImpersonationLevel.Impersonation, rights, 0, share, CreateDisposition.FILE_OPEN, options)
            handles.callback(item.close)
            if item.file_attributes & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                raise NasPermissionDenied("不允许访问 NAS 重解析点")
            parent = item
        assert parent is not None
        connection.operation_timeout = original_timeout
        yield parent


def smb_metadata(flags: int, size: int, modified: datetime) -> NasStat:
    return NasStat(size, bool(flags & FileAttributes.FILE_ATTRIBUTE_DIRECTORY), modified.timestamp())


def smb_entries(handle: Open, deadline: float) -> Iterator[Entry]:
    connection = handle.tree_connect.session.connection
    original = connection.operation_timeout
    try:
        while True:
            check_deadline(deadline)
            connection.operation_timeout = min(original, max(0.1, deadline - monotonic()))
            try:
                rows = handle.query_directory("*", FileInformationClass.FILE_DIRECTORY_INFORMATION, max_output=65536)
            except (NoMoreFiles, NoSuchFile):
                return
            if not rows:
                return
            for row in rows:
                flags = row["file_attributes"].get_value()
                if flags & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                try:
                    name = row["file_name"].get_value().decode("utf-16-le")
                except UnicodeError:
                    continue
                yield Entry(name, smb_metadata(flags, row["end_of_file"].get_value(), row["last_write_time"].get_value()))
    finally:
        connection.operation_timeout = original


class SmbReader(io.RawIOBase):
    def __init__(self, handle: Open, size: int, handles: ExitStack) -> None:
        self.handle, self.size, self.offset = handle, size, 0
        self.handles = handles

    def close(self) -> None:
        try:
            self.handles.close()
        finally:
            super().close()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.offset

    def seek(self, offset: int, whence: int = 0) -> int:
        start = {0: 0, 1: self.offset, 2: self.size}[whence]
        if start + offset < 0:
            raise ValueError("读取偏移不能小于零")
        self.offset = start + offset
        return self.offset

    def readinto(self, buffer: bytearray | memoryview) -> int:
        length = min(len(buffer), self.size - self.offset, self.handle.tree_connect.session.connection.max_read_size)
        if length <= 0:
            return 0
        data = self.handle.read(self.offset, length)
        buffer[:len(data)] = data
        self.offset += len(data)
        return len(data)
