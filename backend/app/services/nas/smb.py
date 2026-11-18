"""每个实例独立缓存、显式凭据；仅安全的读请求允许重放一次。"""
from __future__ import annotations

import errno
import re
import stat
from typing import BinaryIO, Callable, Mapping, TypeVar, cast

import smbclient
from smbprotocol.exceptions import SMBConnectionClosed
from smbprotocol.session import Session

from .errors import NasPermissionDenied, NasSessionExpired, translate
from .fs import BaseFs, NasStat
from .paths import Access, NasPaths, Region, safe_join

T = TypeVar("T")


def disconnected(error: Exception) -> bool:
    return (isinstance(error, (SMBConnectionClosed, ConnectionError, TimeoutError))
            or getattr(error, "errno", None) in (errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT, errno.ENOTCONN)
            or getattr(error, "status", getattr(error, "ntstatus", None)) in (0xC000020C, 0xC000020D, 0xC00000C9))


class SmbFs(BaseFs):
    def __init__(self, host: str, share: str, username: str, password: str,
                 paths: NasPaths | None = None, timeout: float = 30,
                 permissions: Mapping[Region, Access] | None = None) -> None:
        super().__init__(paths or NasPaths(), permissions)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or "/" in safe_join(share):
            raise ValueError("NAS 地址或共享文件夹名称无效")
        if not username or not password:
            raise NasPermissionDenied("SMB 必须提供指定账号和密码")
        self.host = host
        self.share = share
        self.username = username
        self.timeout = timeout
        self._disconnected = False
        self._cache: dict[str, object] = {}
        self._session: Session | None = None
        try:
            self._connect(password)
        except Exception as error:
            self.close()
            raise translate(error) from None

    def _connect(self, password: str) -> None:
        self._session = smbclient.register_session(
            self.host, username=self.username, password=password,
            connection_cache=self._cache, connection_timeout=self.timeout,
            auth_protocol="ntlm", require_signing=True,
        )
        # 已有连接不需要登录口令；连接失效时由本人重新登录。
        self._session.password = None

    def _options(self) -> dict[str, object]:
        return {"username": self.username, "connection_cache": self._cache,
                "connection_timeout": self.timeout, "auth_protocol": "ntlm", "require_signing": True}

    def _check_connection(self) -> None:
        if self._disconnected or self._session is None or not self._session.connection.transport.connected:
            raise NasSessionExpired("SMB 连接已断开，请重新登录")

    def touch(self) -> None:
        with self._lock:
            self._check_connection()
            super().touch()

    def _run(self, action: Callable[[], T]) -> T:
        def checked() -> T:
            try:
                return action()
            except Exception as error:
                if disconnected(error):
                    self._disconnected = True
                    raise NasSessionExpired("SMB 连接已断开，请重新登录") from None
                raise
        return super()._run(checked)

    def _call(self, action: Callable[[], T], retry: bool = False) -> T:
        self._check_connection()
        return action()

    def _unc(self, path: str) -> str:
        # 禁止重解析点/符号链接把受限目录重定向到其他区域或共享。
        prefix = f"\\\\{self.host}\\{self.share}"
        for part in path.split("/"):
            prefix += "\\" + part
            try:
                info = smbclient.lstat(prefix, **self._options())
            except OSError as error:
                from .errors import NasNotFound
                if isinstance(translate(error), NasNotFound):
                    break
                raise
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise NasPermissionDenied("不允许通过 NAS 重解析点访问文件")
        return f"\\\\{self.host}\\{self.share}\\" + path.replace("/", "\\")

    def _list(self, path: str) -> list[str]:
        return smbclient.listdir(self._unc(path), **self._options())

    def _stat(self, path: str) -> NasStat:
        info = smbclient.stat(self._unc(path), follow_symlinks=False, **self._options())
        return NasStat(info.st_size, stat.S_ISDIR(info.st_mode), info.st_mtime)

    def _open_raw(self, path: str, mode: str) -> BinaryIO:
        return cast(BinaryIO, smbclient.open_file(self._unc(path), mode=mode, **self._options()))

    def _mkdir(self, path: str, recursive: bool) -> None:
        if recursive:
            smbclient.makedirs(self._unc(path), exist_ok=False, **self._options())
        else:
            smbclient.mkdir(self._unc(path), **self._options())

    def _rename(self, source: str, target: str) -> None:
        smbclient.rename(self._unc(source), self._unc(target), **self._options())

    def _delete(self, path: str, recursive: bool) -> None:
        target = self._unc(path)
        info = smbclient.stat(target, follow_symlinks=False, **self._options())
        if stat.S_ISDIR(info.st_mode):
            if recursive:
                for name in smbclient.listdir(target, **self._options()):
                    self._delete(safe_join(path, name), True)
            smbclient.rmdir(target, **self._options())
        else:
            smbclient.remove(target, **self._options())

    def _close_backend(self) -> None:
        try:
            smbclient.reset_connection_cache(connection_cache=self._cache, fail_on_error=True)
        finally:
            self._session = None
            self._cache.clear()
