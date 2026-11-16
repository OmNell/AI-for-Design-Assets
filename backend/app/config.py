"""文件与环境变量配置，所有相对文件路径以配置文件目录为基准。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ServerConfig(Section):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    session_hours: int = Field(default=8, gt=0)


class NasConfig(Section):
    host: str = "192.168.0.177"
    dsm_port: int = Field(default=5001, ge=1, le=65535)
    dsm_https: bool = True
    share: str = "设计资产共享"
    root_path: str = "/设计资产"
    temp_path: str = "/工作台临时区"
    backup_path: str = "/_工作台备份"
    system_user: str = "wb-system"
    system_password_enc: SecretStr = SecretStr("")


class UploadConfig(Section):
    chunk_mb: int = Field(default=8, gt=0)
    parallel_files: int = Field(default=3, gt=0)
    full_fingerprint_max_mb: int = Field(default=200, gt=0)


class AiConfig(Section):
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_enc: SecretStr = SecretStr("")
    model_vision: str = ""
    model_text: str = ""
    model_embed: str = ""
    concurrency: int = Field(default=3, gt=0)
    timeout_s: int = Field(default=60, gt=0)
    retries: int = Field(default=3, ge=0)
    stall_alert_min: int = Field(default=30, gt=0)


class ReviewConfig(Section):
    overdue_days: int = Field(default=3, gt=0)


class PurgeConfig(Section):
    cooling_days: int = Field(default=30, gt=0)


class ScheduleConfig(Section):
    inspection: str = "0 2 * * *"
    backup: str = "0 3 * * *"


class PathsConfig(Section):
    data_dir: Path = Path("./data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NASWB_", env_nested_delimiter="__", extra="forbid",
        hide_input_in_errors=True, nested_model_default_partial_update=True,
    )
    mode: Literal["dev", "prod"] = "dev"
    bootstrap_admin: str = ""
    server: ServerConfig = Field(default_factory=ServerConfig)
    nas: NasConfig = Field(default_factory=NasConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    purge: PurgeConfig = Field(default_factory=PurgeConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


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
    return settings
