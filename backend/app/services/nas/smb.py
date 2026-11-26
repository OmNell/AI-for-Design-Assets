"""每个登录独立的签名 SMB 会话。同一连接用于认证和业务读写。"""
from __future__ import annotations

import errno
import re
import stat
import uuid
from contextlib import suppress
from typing import BinaryIO, Callable, Mapping, TypeVar, cast

import smbclient
from smbprotocol.connection import Connection
from smbprotocol.exceptions import SMBConnectionClosed
from smbprotocol.header import Commands, NtStatus
from smbprotocol.tree import TreeConnect
from smbprotocol.session import SMB2SessionSetupResponse, Session, SessionFlags
from smbprotocol.open import DirectoryAccessMask

from .errors import NasLoginError, NasNotFound, NasPermissionDenied, NasSessionExpired, translate
from .fs import BaseFs, NasStat
from .paths import Access, NasPaths, Region, safe_join

T = TypeVar("T")


def disconnected(error: Exception) -> bool:
    return (isinstance(error, (SMBConnectionClosed, ConnectionError, TimeoutError))
            or getattr(error, "errno", None) in (errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT, errno.ENOTCONN)
            or getattr(error, "status", getattr(error, "ntstatus", None)) in (
                NtStatus.STATUS_USER_SESSION_DELETED, 0xC000035C, 0xC000020C, 0xC000020D, 0xC00000C9))


class DirectConnection(Connection):
    """连接只在固定服务器上收发，不解析 SMB 符号链接重定向。"""
    def __init__(self, host: str, timeout: float) -> None:
        super().__init__(uuid.uuid4(), host, require_signing=True)
        self.operation_timeout = timeout
        self.bound_to_share = False

    def send(self, message, *args, **kwargs):
        if self.bound_to_share:
            if message.COMMAND == Commands.SMB2_SESSION_SETUP:
                raise NasSessionExpired("SMB 连接已失效，请重新登录")
            if message.COMMAND == Commands.SMB2_TREE_CONNECT:
                raise NasPermissionDenied("不允许重连或访问其他 SMB 共享")
        return super().send(message, *args, **kwargs)

    def receive(self, request, wait=True, timeout=None, resolve_symlinks=True):
        result = super().receive(request, wait, min(timeout or self.operation_timeout, self.operation_timeout), False)
        if (request.message["command"].get_value() == Commands.SMB2_SESSION_SETUP
                and result["status"].get_value() == NtStatus.STATUS_SUCCESS):
            setup = SMB2SessionSetupResponse()
            setup.unpack(result["data"].get_value())
            flags = setup["session_flags"].get_value()
            if flags & (SessionFlags.SMB2_SESSION_FLAG_IS_GUEST | SessionFlags.SMB2_SESSION_FLAG_IS_NULL):
                raise NasLoginError("nas_guest_rejected", "SMB 返回来宾或匿名会话，请使用本人 NAS 账号", 401)
        return result


def login_failure(error: Exception, phase: str) -> NasLoginError:
    if isinstance(error, NasLoginError):
        return error
    status = getattr(error, "status", getattr(error, "ntstatus", None))
    # smbprotocol 的 TCP 层会将套接字错误放在 ValueError 的异常链中。
    os_error = error.__cause__ if isinstance(error.__cause__, OSError) else error
    if getattr(os_error, "errno", None) == errno.ECONNREFUSED or status == NtStatus.STATUS_SERVER_UNAVAILABLE:
        return NasLoginError("smb_unavailable", "NAS 的 SMB 服务暂不可用", 503)
    if isinstance(os_error, OSError) and status is None:
        return NasLoginError("nas_network_unreachable", "无法建立或维持与 NAS 的网络连接", 503)
    if phase == "negotiate":
        if isinstance(error, SMBConnectionClosed):
            return NasLoginError("smb_unavailable", "NAS 的 SMB 服务暂不可用", 503)
        return NasLoginError("smb_negotiation_failed", "SMB 协议协商失败，请检查 NAS 的 SMB 设置", 503)
    if isinstance(error, SMBConnectionClosed) or status == NtStatus.STATUS_IO_TIMEOUT:
        return NasLoginError("nas_network_unreachable", "无法建立或维持与 NAS 的网络连接", 503)
    if phase == "share":
        if status == NtStatus.STATUS_BAD_NETWORK_NAME:
            return NasLoginError("nas_share_not_found", "工作台配置的 SMB 共享不存在", 503)
        if status in (NtStatus.STATUS_ACCESS_DENIED, NtStatus.STATUS_PRIVILEGE_NOT_HELD):
            return NasLoginError("nas_share_permission_denied", "账号无法访问工作台配置的 NAS 共享", 403)
        return NasLoginError("smb_share_unavailable", "无法建立 SMB 共享连接，请检查共享设置", 503)
    if status in (0xC0000064, NtStatus.STATUS_WRONG_PASSWORD, NtStatus.STATUS_LOGON_FAILURE):
        return NasLoginError("invalid_credentials", "NAS 账号或密码不正确", 401)
    # 账号限制、登录时间、工作站限制、密码过期、账号停用/过期、须改密、账号锁定。
    if status in (0xC000006E, 0xC000006F, 0xC0000070, NtStatus.STATUS_PASSWORD_EXPIRED, 0xC0000072,
                  0xC000015B, 0xC0000193, 0xC0000224, 0xC0000234):
        return NasLoginError("nas_account_restricted", "NAS 账号已停用或受到登录限制，请联系 NAS 管理员", 403)
    return NasLoginError("smb_auth_failed", "SMB 身份验证未能完成，请检查 NAS 认证设置", 503)


def direct_session(host: str, share: str, username: str, password: str, timeout: float) -> TreeConnect:
    if not username or not password:
        raise NasLoginError("invalid_credentials", "SMB 必须提供指定账号和密码", 401)
    connection = DirectConnection(host, timeout)
    phase = "negotiate"
    try:
        connection.connect(timeout=timeout)
        phase = "authenticate"
        session = Session(connection, username=username, password=password, require_encryption=False, auth_protocol="ntlm")
        try:
            session.connect()
        finally:
            session.password = None
        if not session.session_id or not session.session_key or not (session.signing_required or session.encrypt_data):
            raise NasLoginError("smb_auth_failed", "SMB 未建立可验证的本人会话", 503)
        phase = "share"
        tree = TreeConnect(session, rf"\\{host}\{share}")
        tree.connect()
        if tree.is_dfs_share:
            raise NasLoginError("smb_dfs_unsupported", "工作台仅支持直连 SMB 共享", 503)
        connection.bound_to_share = True
        return tree
    except Exception as error:
        if connection.transport is not None:
            with suppress(Exception):
                connection.disconnect(close=False, timeout=timeout)
        raise login_failure(error, phase) from None


class SessionCache(dict[str, DirectConnection]):
    """供 smbclient 复用既有本人会话，拒绝默认连接和自动重新认证。"""
    def get(self, key, default=None):
        connection = super().get(key)
        if connection is None:
            raise NasPermissionDenied("不允许访问其他 SMB 服务器")
        if not connection.transport.connected:
            raise NasSessionExpired("SMB 连接已断开，请重新登录")
        return connection

    def __setitem__(self, key, connection):
        # 连接可能在 smbclient 取出缓存后立即中断。禁止新连接进入缓存。
        with suppress(Exception):
            connection.disconnect(close=False)
        raise NasSessionExpired("SMB 连接已断开，请重新登录")


class SmbFs(BaseFs):
    def __init__(self, host: str, share: str, username: str, password: str,
                 paths: NasPaths | None = None, timeout: float = 30,
                 permissions: Mapping[Region, Access] | None = None) -> None:
        super().__init__(paths or NasPaths(), permissions)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or "/" in safe_join(share):
            raise ValueError("NAS 地址或共享文件夹名称无效")
        self.host = host
        self.share = share
        self.username = username
        self.timeout = timeout
        self._disconnected = False
        tree = direct_session(host, share, username, password, timeout)
        self.browser_tree: TreeConnect | None = tree
        self._session: Session | None = tree.session
        self._cache = SessionCache({f"{host.lower()}:445": tree.session.connection})

    def _options(self) -> dict[str, object]:
        return {"username": self.username, "connection_cache": self._cache,
                "connection_timeout": self.timeout, "auth_protocol": "ntlm", "require_signing": True}

    def _check_connection(self) -> None:
        if (self._disconnected or self._session is None or self.browser_tree is None
                or not self._session.connection.transport.connected):
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
        options = self._options()
        if mode == "rb":
            # 同一文件可以同时供多位访问者读取。传输期间拒绝更改或删除已打开的文件。
            options["share_access"] = "r"
        return cast(BinaryIO, smbclient.open_file(self._unc(path), mode=mode, **options))

    def _mkdir(self, path: str, recursive: bool) -> None:
        if recursive:
            smbclient.makedirs(self._unc(path), exist_ok=False, **self._options())
        else:
            smbclient.mkdir(self._unc(path), **self._options())

    def _rename(self, source: str, target: str) -> None:
        smbclient.rename(self._unc(source), self._unc(target), **self._options())

    def _check_write_directory(self, path: str) -> None:
        rights = DirectoryAccessMask.FILE_ADD_FILE | DirectoryAccessMask.FILE_ADD_SUBDIRECTORY
        with smbclient.open_file(self._unc(path), mode="rb", buffering=0, desired_access=rights,
                                 file_type="dir", share_access="rwd", **self._options()):
            pass

    def _replace(self, source: str, target: str) -> None:
        smbclient.replace(self._unc(source), self._unc(target), **self._options())

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
        session = self._session
        try:
            if session is not None:
                connection = session.connection
                try:
                    connection.disconnect(timeout=self.timeout)
                except Exception:
                    with suppress(Exception):
                        connection.disconnect(close=False, timeout=self.timeout)
                    raise
        finally:
            self._session = None
            self.browser_tree = None
            self._cache.clear()
