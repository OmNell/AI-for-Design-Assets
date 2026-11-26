"""NAS 对上层公开的异常，不携带认证材料或上游请求对象。"""
from __future__ import annotations

import errno

from smbprotocol.exceptions import SMBAuthenticationError


class NasError(Exception):
    pass


class NasPermissionDenied(NasError):
    pass


class NasNotFound(NasError):
    pass


class NasAlreadyExists(NasError):
    pass


class NasUnavailable(NasError):
    pass


class NasLoginError(NasUnavailable):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class NasSessionExpired(NasError):
    pass


def translate(error: Exception) -> NasError:
    if isinstance(error, NasError):
        return error
    status = getattr(error, "ntstatus", getattr(error, "status", None))
    number = getattr(error, "errno", None)
    if isinstance(error, SMBAuthenticationError):
        return NasPermissionDenied("SMB 身份验证失败")
    if status is not None:
        # SMB 的共享冲突也可能映射到 EPERM，必须优先按 NTSTATUS 分类。
        if status in (0xC0000022, 0xC0000061, 0xC000006D, 0xC0000121):
            return NasPermissionDenied("NAS 无权限")
        if status in (0xC0000034, 0xC000003A, 0xC000000F):
            return NasNotFound("NAS 文件或目录不存在")
        if status == 0xC0000035:
            return NasAlreadyExists("NAS 文件或目录已存在")
        return NasUnavailable("NAS 操作失败或连接不可用")
    if isinstance(error, PermissionError) or number in (errno.EACCES, errno.EPERM):
        return NasPermissionDenied("NAS 无权限")
    if isinstance(error, FileNotFoundError) or number == errno.ENOENT:
        return NasNotFound("NAS 文件或目录不存在")
    if isinstance(error, FileExistsError) or number == errno.EEXIST:
        return NasAlreadyExists("NAS 文件或目录已存在")
    return NasUnavailable("NAS 操作失败或连接不可用")
