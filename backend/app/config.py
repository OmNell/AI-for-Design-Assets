"""文件与环境变量配置，所有相对文件路径以配置文件目录为基准。"""
from __future__ import annotations

import os
import ipaddress
import re
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

LEGACY_DSM_KEYS = frozenset({"dsm_port", "dsm_https", "dsm_verify_ssl", "dsm_ca_file"})


def without_legacy_dsm(nas: dict[str, Any]) -> dict[str, Any]:
    """允许 V1.1.5 的 DSM 字段随配置读取，SMB 不使用这些旧设置。"""
    return {key: value for key, value in nas.items() if key not in LEGACY_DSM_KEYS}


def has_nas_env_override() -> bool:
    obsolete = {f"NASWB_NAS__{key.upper()}" for key in LEGACY_DSM_KEYS}
    return any(key.upper() not in obsolete and
               (key.upper() == "NASWB_NAS" or key.upper().startswith("NASWB_NAS__"))
               for key in os.environ)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ServerConfig(Section):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    session_hours: int = Field(default=8, gt=0)
    public_base_url: str = "http://nas-workbench.company.local:8080"

    @model_validator(mode="after")
    def validate_public_url(self) -> ServerConfig:
        url = urlsplit(self.public_base_url)
        host = url.hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("永久链接必须使用固定域名")
        if (url.scheme not in ("http", "https") or not re.fullmatch(r"[a-z0-9.-]+", host)
                or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("永久链接地址必须使用无路径的 HTTP 或 HTTPS 域名")
        if url.port == 0:
            raise ValueError("永久链接端口不合法")
        return self


class NasConfig(Section):
    host: str = "192.168.0.177"
    share: str = "设计资产共享"
    work_root: str | None = None
    verified_address: ipaddress.IPv4Address | None = None
    root_path: str = "/设计资产"
    temp_path: str = "/工作台临时区"
    backup_path: str = "/_工作台备份"
    idle_minutes: float = Field(default=30, gt=0, allow_inf_nan=False)
    timeout_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    system_user: str = "wb-system"
    system_password_enc: SecretStr = SecretStr("")

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_dsm(cls, values: Any) -> Any:
        return without_legacy_dsm(values) if isinstance(values, dict) else values


class UploadConfig(Section):
    chunk_mb: int = Field(default=8, gt=0)
    append_max_mb: int = Field(default=50, gt=0)
    parallel_files: int = Field(default=3, gt=0)
    fingerprint_algorithm: Literal["sha256"] = "sha256"
    # 只接受旧配置键，计算逻辑始终使用完整 SHA-256。
    full_fingerprint_max_mb: int | None = Field(default=None, gt=0, exclude=True)


class JobsConfig(Section):
    workers: int = Field(default=4, ge=1, le=32)


class AiConfig(Section):
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_enc: SecretStr = SecretStr("")
    model_vision: str = ""
    model_text: str = ""
    model_text_version: str = ""
    model_embed: str = ""
    model_embed_version: str = ""
    concurrency: int = Field(default=3, gt=0)
    timeout_s: int = Field(default=60, gt=0)
    retries: int = Field(default=3, ge=0)
    stall_alert_min: int = Field(default=30, gt=0)


class ReviewConfig(Section):
    overdue_days: int = Field(default=3, gt=0)
    self_review_admin: str = Field(default="", max_length=128)


class PurgeConfig(Section):
    cooling_days: int = Field(default=30, ge=30)


class ScheduleConfig(Section):
    inspection: str = "0 2 * * *"
    backup: str = "0 3 * * *"


class RecoveryConfig(Section):
    read_only: bool = False


class PathsConfig(Section):
    data_dir: Path = Path("./data")
    dev_nas_dir: Path = Path("./devdata/nas")


class DevUser(Section):
    username: str = Field(min_length=1)
    password: SecretStr
    nas_browser_read: bool = False
    permissions: dict[Literal["formal", "temp", "backup"], Literal["none", "read", "write"]] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NASWB_", env_nested_delimiter="__", extra="forbid",
        hide_input_in_errors=True, nested_model_default_partial_update=True,
    )
    mode: Literal["dev", "prod"] = "dev"
    bootstrap_admin: str = ""
    dev_users: list[DevUser] = Field(default_factory=list)
    server: ServerConfig = Field(default_factory=ServerConfig)
    nas: NasConfig = Field(default_factory=NasConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    purge: PurgeConfig = Field(default_factory=PurgeConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)

    @model_validator(mode="after")
    def unique_dev_users(self) -> Settings:
        names = [user.username for user in self.dev_users]
        if len(set(names)) != len(names):
            raise ValueError("开发账号不能重复")
        return self


def load_settings(config_file: Path | str | None = None) -> Settings:
    path = Path(config_file or os.environ.get("NASWB_CONFIG", "config.yaml")).resolve()
    if not path.is_file():
        raise FileNotFoundError("配置文件不存在，请先从 backend/config.example.yaml 复制 config.yaml")

    class FileSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource, dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_file=path, yaml_file_encoding="utf-8"))

    settings = FileSettings()
    if not settings.paths.data_dir.is_absolute():
        settings.paths.data_dir = (path.parent / settings.paths.data_dir).resolve()
    if not settings.paths.dev_nas_dir.is_absolute():
        settings.paths.dev_nas_dir = (path.parent / settings.paths.dev_nas_dir).resolve()
    return settings
