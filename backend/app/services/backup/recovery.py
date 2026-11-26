"""核验备份并在独立目录准备跨环境恢复。NAS 只读。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from zipfile import BadZipFile, ZipFile

from alembic.util import CommandError
from pydantic_settings import PydanticBaseSettingsSource
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
import yaml

from app.config import AiConfig, Settings, load_settings
from app.db.database import Database
from app.db.models import Setting, UserSession, utcnow
from app.services.crypto import CryptoError, SecretCipher, restrict_key_permissions
from app.services.maintenance.settings import ASSET_RECOVERY_CONFIRMED
from app.services.nas import NasFs, NasNotFound, safe_join
from app.services.nas.factory import create_paths

from .format import BACKUP_NAME, BLOCK, FILES, inventory, measure, read_manifest


class BackupRecoveryError(ValueError):
    """可向管理员展示的备份验证、迁移或配置错误。"""


class BackupSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls: type[Settings], init_settings: PydanticBaseSettingsSource,
                                   env_settings: PydanticBaseSettingsSource, dotenv_settings: PydanticBaseSettingsSource,
                                   file_secret_settings: PydanticBaseSettingsSource) -> tuple[PydanticBaseSettingsSource, ...]:
        # 不允许现行环境变量遮盖归档中的密文或原始配置。
        return (init_settings,)


def _file_settings(path: Path) -> Settings:
    return BackupSettings.model_validate(yaml.safe_load(path.read_bytes()))


def _backup_key(data: Path, source_config: Settings) -> SecretCipher:
    """验证备份中的实际密文与原密钥匹配。"""
    try:
        cipher = SecretCipher(data, read_only=True)
        for secret in (source_config.nas.system_password_enc, source_config.ai.api_key_enc):
            if secret.get_secret_value():
                cipher.decrypt(secret.get_secret_value())
        uri = (data / "app.db").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            for key, value, encrypted in db.execute("SELECT key,value,encrypted FROM settings"):
                if key == "ai":
                    saved = AiConfig.model_validate(json.loads(value))
                    token = saved.api_key_enc.get_secret_value()
                    if encrypted and not token:
                        raise BackupRecoveryError("备份数据库的加密配置不完整")
                    if token:
                        cipher.decrypt(token)
                elif encrypted:
                    raise BackupRecoveryError("备份含有当前版本不支持核验的加密设置")
        return cipher
    except CryptoError:
        raise BackupRecoveryError("备份加密设置与原密钥无法匹配") from None


def _private_file(path: Path, contents: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as writer:
        if os.name == "nt":
            restrict_key_permissions(path)
        writer.write(contents)


def restore(package: Path, target: Path) -> dict:
    """原样解包用于 Windows 升级回滚及离线核验，不改原数据库结构。"""
    if target.exists() or target.is_symlink():
        raise BackupRecoveryError("目标目录必须不存在，禁止覆盖已运行或已有的工作台")
    with ZipFile(package) as archive:
        manifest = read_manifest(archive)
        if sum(entry["size"] for entry in manifest["files"].values()) >= shutil.disk_usage(target.parent).free:
            raise BackupRecoveryError("本地剩余空间不足以解包备份")
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
        try:
            data = target / "data"
            data.mkdir(mode=0o700)
            for name in FILES:
                destination = (target if name == "config.yaml" else data) / name
                fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as writer, archive.open(name) as reader:
                    if os.name == "nt":
                        restrict_key_permissions(destination)
                    shutil.copyfileobj(reader, writer, BLOCK)
                with destination.open("rb") as stream:
                    expected = manifest["files"][name]
                    if measure(stream) != (expected["size"], expected["sha256"]):
                        raise BackupRecoveryError("备份成员指纹与清单不一致")
            identities = inventory(data / "app.db")
            if manifest["identities"] != identities:
                raise BackupRecoveryError("项目、商品、文件、永久链接或向量的恢复结果不一致")
            _backup_key(data, _file_settings(target / "config.yaml"))
            return identities
        except BaseException:
            shutil.rmtree(target)
            raise


def _binding(settings: Settings) -> tuple:
    nas = settings.nas
    return nas.host.casefold(), nas.verified_address, nas.share.casefold(), nas.system_user.casefold(), create_paths(settings)


def _runtime_config(source: Settings, destination: Settings | None, cipher: SecretCipher) -> dict:
    config = (destination or source).model_copy(deep=True)
    if destination is None:
        config.server.host = "127.0.0.1"
    else:
        if destination.mode != source.mode:
            raise BackupRecoveryError("备份运行模式与目标环境不一致")
        # 当前地址可以变化，目录根必须对应原有批次与资产路径。
        if create_paths(destination) != create_paths(source):
            raise BackupRecoveryError("备份 NAS 区域路径与目标环境不一致")
    config.paths.data_dir = Path("./data")
    config.recovery.read_only = True
    if destination is None:
        config.paths.dev_nas_dir = Path("./devdata/nas")
    raw = config.model_dump(mode="json")
    raw["dev_users"] = [{**row.model_dump(mode="json"), "password": row.password.get_secret_value()}
                        for row in config.dev_users] if config.mode == "dev" else []
    original_nas_key = source.nas.system_password_enc.get_secret_value()
    original_ai_key = source.ai.api_key_enc.get_secret_value()
    destination_nas_key = config.nas.system_password_enc.get_secret_value()
    destination_ai_key = config.ai.api_key_enc.get_secret_value()
    if destination is None:
        nas_key, ai_key = original_nas_key, original_ai_key
    else:
        destination_cipher = SecretCipher(destination.paths.data_dir, read_only=True) if destination_nas_key or destination_ai_key else None
        nas_key = (cipher.encrypt(destination_cipher.decrypt(destination_nas_key)) if destination_nas_key
                   else original_nas_key if _binding(source) == _binding(destination) else "")
        ai_key = (cipher.encrypt(destination_cipher.decrypt(destination_ai_key)) if destination_ai_key else "")
        if not any((destination_ai_key, config.ai.model_vision, config.ai.model_text, config.ai.model_embed)):
            raw["ai"] = source.ai.model_dump(mode="json")
            ai_key = original_ai_key
    raw["nas"]["system_password_enc"] = nas_key
    raw["ai"]["api_key_enc"] = ai_key
    return raw


def _revision(db: Path) -> str:
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            raise BackupRecoveryError("备份缺少数据库迁移版本")
        return row[0]


def stage_backup(package: Path, target: Path, *, destination_config: Settings | Path | None = None) -> dict:
    """迁移隔离库并废止备份中的登录令牌，保留原文件和业务历史。"""
    destination = load_settings(destination_config) if isinstance(destination_config, Path) else destination_config
    if destination is not None:
        active = destination.paths.data_dir.resolve()
        stage = target.resolve()
        if stage == active or stage in active.parents or active in stage.parents:
            raise BackupRecoveryError("备份恢复目录必须与运行数据目录独立")
    original_inventory = restore(package, target)
    try:
        config = target / "config.yaml"
        data = target / "data"
        if (data / "app.db").stat().st_size * 2 >= shutil.disk_usage(data).free:
            raise BackupRecoveryError("本地剩余空间不足以安全迁移数据库")
        source = _file_settings(config)
        cipher = SecretCipher(data, read_only=True)
        raw = _runtime_config(source, destination, cipher)
        original_revision = _revision(data / "app.db")
        database = Database(data)
        try:
            try:
                database.migrate()
            except (CommandError, sqlite3.DatabaseError, SQLAlchemyError):
                raise BackupRecoveryError("备份数据库版本不兼容或无法迁移") from None
            with database.session() as session:
                revoked = session.execute(update(UserSession).where(UserSession.revoked_at.is_(None))
                                          .values(revoked_at=utcnow())).rowcount
                confirmation = session.scalar(select(Setting).where(Setting.key == ASSET_RECOVERY_CONFIRMED))
                if confirmation is None:
                    session.add(Setting(key=ASSET_RECOVERY_CONFIRMED, value=False, encrypted=False))
                else:
                    confirmation.value = False
        finally:
            database.close()
        staged_inventory = inventory(data / "app.db")
        _backup_key(data, BackupSettings.model_validate(raw))
        config.rename(target / "source-config.yaml")
        _private_file(config, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode())
        report = {"source_revision": original_revision, "revision": _revision(data / "app.db"),
                  "source_inventory": original_inventory, "inventory": staged_inventory, "sessions_revoked": revoked}
        _private_file(target / "restore-report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
        return report
    except BaseException:
        shutil.rmtree(target)
        raise


def list_candidates(fs: NasFs, backup_root: str, *, verify_manifest: bool = False) -> list[dict]:
    """按备份命名时间列出最近候选，仅在明确请求时读取候选清单。"""
    candidates = []
    names = sorted((name for name in fs.list(backup_root) if BACKUP_NAME.fullmatch(name)), reverse=True)
    for name in names[:20]:
        path = safe_join(backup_root, name)
        try:
            info = fs.stat(path)
        except NasNotFound:
            continue
        if info.is_dir:
            continue
        validation, version, projects, app_version = "unverified", None, None, None
        if verify_manifest and info.size < 128:
            validation = "invalid"
        elif verify_manifest:
            try:
                with fs.open_read(path) as stream, ZipFile(stream) as archive:
                    manifest = read_manifest(archive)
                version = manifest["version"]
                app_version = manifest.get("app_version")
                projects = manifest["identities"].get("projects", {}).get("count")
                if type(projects) is not int or projects < 0:
                    raise BackupRecoveryError("备份清单缺少有效的项目数量")
                validation = "empty" if projects == 0 else "manifest_checked"
            except NasNotFound:
                continue
            except (BadZipFile, ValueError, NotImplementedError, AttributeError):
                validation, projects = "invalid", None
        candidates.append({"name": name, "size": info.size, "format": version, "app_version": app_version,
                           "validation": validation, "projects": projects,
                           "modified_at": datetime.fromtimestamp(info.mtime, timezone.utc).isoformat()})
    return candidates


def stage_from_nas(fs: NasFs, backup_root: str, filename: str, target: Path, *,
                   destination_config: Settings | Path | None = None) -> dict:
    if not BACKUP_NAME.fullmatch(filename):
        raise BackupRecoveryError("请选择工作台格式的备份文件")
    path = safe_join(backup_root, filename)
    expected = fs.stat(path)
    if expected.is_dir or expected.size <= 0 or expected.size >= shutil.disk_usage(target.parent).free:
        raise BackupRecoveryError("备份文件不合法或本地剩余空间不足")
    with tempfile.TemporaryDirectory(prefix="wb-backup-", dir=target.parent) as folder:
        local = Path(folder) / "backup.zip"
        with fs.open_read(path) as reader, local.open("xb") as writer:
            remaining = expected.size
            while remaining and (block := reader.read(min(BLOCK, remaining))):
                writer.write(block)
                remaining -= len(block)
            if remaining or reader.read(1) or fs.stat(path) != expected:
                raise BackupRecoveryError("备份文件在下载过程中变化")
        return stage_backup(local, target, destination_config=destination_config)
