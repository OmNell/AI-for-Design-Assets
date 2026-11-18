"""NAS 接口、流式文件句柄及会话生命周期。"""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping, Protocol, TypeVar

from .errors import NasNotFound, NasPermissionDenied, NasUnavailable, translate
from .paths import Access, NasPaths, Region

T = TypeVar("T")


@dataclass(frozen=True)
class NasStat:
    size: int
    is_dir: bool
    mtime: float


class NasFs(Protocol):
    def list(self, path: str) -> list[str]: ...
    def stat(self, path: str) -> NasStat: ...
    def exists(self, path: str) -> bool: ...
    def open_read(self, path: str, offset: int = 0) -> BinaryIO: ...
    def open_write(self, path: str, append: bool = False) -> BinaryIO: ...
    def mkdir(self, path: str, recursive: bool = False) -> None: ...
    def rename(self, source: str, target: str) -> None: ...
    def check_write_directory(self, path: str) -> None: ...
    def replace(self, source: str, target: str) -> None: ...
    def delete(self, path: str, recursive: bool = False) -> None: ...
    def close(self) -> None: ...


class NasStream(io.BufferedIOBase):
    """流操作也统一异常；不重试已开始的写入，避免重复追加。"""
    def __init__(self, owner: BaseFs, stream: BinaryIO) -> None:
        self.owner = owner
        self.stream = stream
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self, size: int = -1) -> bytes:
        return self.owner._run(lambda: self.stream.read(size))

    def write(self, data: bytes) -> int:
        return self.owner._run(lambda: self.stream.write(data))

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.owner._run(lambda: self.stream.seek(offset, whence))

    def truncate(self, size: int | None = None) -> int:
        return self.owner._run(lambda: self.stream.truncate(size))

    def tell(self) -> int:
        return self.owner._run(self.stream.tell)

    def flush(self) -> None:
        if not self._closed:
            self.owner._run(self.stream.flush)

    def readable(self) -> bool:
        return self.stream.readable()

    def writable(self) -> bool:
        return self.stream.writable()

    def seekable(self) -> bool:
        return self.stream.seekable()

    def close(self) -> None:
        with self.owner._lock:
            if self._closed:
                return
            try:
                self.stream.close()
            except Exception as error:
                raise translate(error) from None
            finally:
                self._closed = True
                self.owner._streams.discard(self)
                self.owner._last_used = time.monotonic()


class BaseFs:
    def __init__(self, paths: NasPaths, permissions: Mapping[Region, Access] | None = None) -> None:
        self.paths = paths
        self.permissions = {Region(region): Access(access) for region, access in permissions.items()} if permissions is not None else None
        self._lock = threading.RLock()
        self._closed = False
        self._streams: set[NasStream] = set()
        self._last_used = time.monotonic()

    def _run(self, action: Callable[[], T]) -> T:
        with self._lock:
            if self._closed:
                raise NasUnavailable("NAS 会话已关闭，请重新登录")
            self._last_used = time.monotonic()
            try:
                return action()
            except Exception as error:
                raise translate(error) from None
            finally:
                self._last_used = time.monotonic()

    def touch(self) -> None:
        self._run(lambda: None)

    def expire(self, idle_seconds: float) -> bool:
        with self._lock:
            if self._closed:
                return True
            if not self._streams and time.monotonic() - self._last_used > idle_seconds:
                self.close()
                return True
            return False

    def _check(self, path: str, write: bool = False, destructive: bool = False) -> str:
        path = self.paths.check(path, self.permissions, write)
        if destructive and path == self.paths.root(self.paths.region(path)):
            raise NasPermissionDenied("禁止删除或改名区域根目录")
        return path

    def _call(self, action: Callable[[], T], retry: bool = False) -> T:
        return action()

    def list(self, path: str) -> list[str]:
        return self._run(lambda: self._call(lambda: sorted(self._list(self._check(path))), retry=True))

    def stat(self, path: str) -> NasStat:
        return self._run(lambda: self._call(lambda: self._stat(self._check(path)), retry=True))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NasNotFound:
            return False

    def _open(self, path: str, mode: str, offset: int = 0) -> BinaryIO:
        def action() -> BinaryIO:
            checked = self._check(path, write=mode != "rb")
            # 仅建立句柄前可做重连；seek/读写失败不能自动重放。
            raw = self._call(lambda: self._open_raw(checked, mode), retry=mode == "rb")
            try:
                if offset:
                    raw.seek(offset)
                result = NasStream(self, raw)
                self._streams.add(result)
                return result
            except Exception:
                raw.close()
                raise
        return self._run(action)

    def open_read(self, path: str, offset: int = 0) -> BinaryIO:
        if offset < 0:
            raise ValueError("读取偏移不能小于零")
        return self._open(path, "rb", offset)

    def open_write(self, path: str, append: bool = False) -> BinaryIO:
        return self._open(path, "ab" if append else "wb")

    def mkdir(self, path: str, recursive: bool = False) -> None:
        self._run(lambda: self._call(lambda: self._mkdir(self._check(path, True), recursive)))

    def rename(self, source: str, target: str) -> None:
        def action() -> None:
            src = self._check(source, True, True)
            dst = self._check(target, True, True)
            self._call(lambda: self._rename(src, dst))
        self._run(action)

    def check_write_directory(self, path: str) -> None:
        self._run(lambda: self._call(lambda: self._check_write_directory(self._check(path, True))))

    def replace(self, source: str, target: str) -> None:
        def action() -> None:
            src = self._check(source, True, True)
            dst = self._check(target, True, True)
            self._call(lambda: self._replace(src, dst))
        self._run(action)

    def _check_write_directory(self, path: str) -> None:
        if not self._stat(path).is_dir:
            raise NasPermissionDenied("正式目录位置不是目录")

    def _replace(self, source: str, target: str) -> None:
        raise NotImplementedError

    def delete(self, path: str, recursive: bool = False) -> None:
        # 删除与 rename 不自动重放；服务端是否已提交可能不确定。
        self._run(lambda: self._call(lambda: self._delete(self._check(path, True, True), recursive)))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            failed = False
            try:
                for stream in tuple(self._streams):
                    try:
                        stream.close()
                    except Exception:
                        failed = True
                self._close_backend()
            except Exception:
                failed = True
            finally:
                self._closed = True
            if failed:
                raise NasUnavailable("NAS 会话清理失败，连接已作废")

    def _list(self, path: str) -> list[str]:
        raise NotImplementedError

    def _stat(self, path: str) -> NasStat:
        raise NotImplementedError

    def _open_raw(self, path: str, mode: str) -> BinaryIO:
        raise NotImplementedError

    def _mkdir(self, path: str, recursive: bool) -> None:
        raise NotImplementedError

    def _rename(self, source: str, target: str) -> None:
        raise NotImplementedError

    def _delete(self, path: str, recursive: bool) -> None:
        raise NotImplementedError

    def _close_backend(self) -> None:
        pass
