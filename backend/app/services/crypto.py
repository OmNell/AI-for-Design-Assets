"""本机密钥与 Fernet 加密；不记录明文、密文和密钥。"""
from __future__ import annotations

import os
import csv
import ctypes
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(Exception):
    pass


class SecretCipher:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "secret.key"
        try:
            # 独占创建，不能覆盖已有密钥。单进程部署，不支持多个实例同时初始化。
            with key_path.open("xb") as stream:
                restrict_key_permissions(key_path)
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        restrict_key_permissions(key_path)
        try:
            self._fernet = Fernet(key_path.read_bytes())
        except (OSError, ValueError):
            raise CryptoError("加密密钥不可用，不能自动覆盖；请检查权限或恢复原密钥") from None

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise CryptoError("解密失败，请检查密文及加密密钥") from None


def restrict_key_permissions(path: Path) -> None:
    """POSIX 仅当前用户；Windows 仅服务身份、管理员组和 SYSTEM。"""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    try:
        from ctypes import wintypes
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        sid = next(csv.reader(identity.stdout.strip().splitlines()))[1]
        if not sid.startswith("S-1-") or any(c not in "S-0123456789" for c in sid):
            raise ValueError("无效的本机身份")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        convert.restype = wintypes.BOOL
        apply = advapi.SetFileSecurityW
        apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        apply.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{sid})"
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError("无法生成密钥访问控制表")
        try:
            if not apply(str(path), 0x80000004, descriptor):
                raise OSError("无法限制密钥访问权限")
        finally:
            kernel.LocalFree(descriptor)
    except Exception:
        raise CryptoError("无法限制密钥访问权限，请使用支持 ACL 的本地磁盘和服务账号") from None
