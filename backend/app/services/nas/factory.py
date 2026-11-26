"""按运行模式创建本人文件会话；系统会话使用独立入口及固定权限。"""
from __future__ import annotations

from app.config import Settings
from app.services.auth.backends import FakeAuthBackend
from app.services.crypto import SecretCipher

from .errors import NasLoginError, NasPermissionDenied
from .fs import BaseFs
from .local import LocalFs
from .paths import Access, NasPaths, Region, SYSTEM_PERMISSIONS
from .pool import SessionPool
from .smb import SmbFs


def create_paths(config: Settings) -> NasPaths:
    paths = NasPaths(config.nas.root_path, config.nas.temp_path, config.nas.backup_path)
    if config.nas.work_root is not None and NasPaths.under(config.nas.work_root) != paths:
        raise ValueError("工作目录与三个派生区域不一致")
    return paths


def create_fs(config: Settings, username: str, password: str) -> BaseFs:
    paths = create_paths(config)
    if config.mode == "prod":
        return SmbFs(str(config.nas.verified_address or config.nas.host), config.nas.share, username, password, paths, config.nas.timeout_s)
    if not FakeAuthBackend(config.dev_users).verify(username, password):
        raise NasLoginError("invalid_credentials", "账号或密码不正确", 401)
    user = next(user for user in config.dev_users if user.username == username)
    permissions = {Region(region): Access(access) for region, access in user.permissions.items()}
    return LocalFs(config.paths.dev_nas_dir, permissions, paths, initialize=config.nas.work_root is None,
                   browser_read=user.nas_browser_read)


def create_system_fs(config: Settings) -> BaseFs:
    paths = create_paths(config)
    if config.mode == "dev":
        return LocalFs(config.paths.dev_nas_dir, SYSTEM_PERMISSIONS, paths, initialize=config.nas.work_root is None)
    encrypted = config.nas.system_password_enc.get_secret_value()
    if not encrypted:
        raise NasPermissionDenied("尚未配置加密的系统账号密码")
    password = SecretCipher(config.paths.data_dir, read_only=True).decrypt(encrypted)
    try:
        return SmbFs(str(config.nas.verified_address or config.nas.host), config.nas.share, config.nas.system_user, password,
                     paths, config.nas.timeout_s, SYSTEM_PERMISSIONS)
    finally:
        password = ""


def create_session_pool(config: Settings) -> SessionPool:
    # 拷贝配置，调用方后续修改不能改变已创建池的账号和权限。
    snapshot = config.model_copy(deep=True)
    return SessionPool(lambda username, password: create_fs(snapshot, username, password),
                       lambda: create_system_fs(snapshot), snapshot.nas.idle_minutes)
