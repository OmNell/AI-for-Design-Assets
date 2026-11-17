"""文件与环境变量配置，所有相对文件路径以配置文件目录为基准。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
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
    idle_minutes: float = Field(default=30, gt=0, allow_inf_nan=False)
    timeout_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    dsm_ca_file: Path | None = None
    system_user: str = "wb-system"
    system_password_enc: SecretStr = SecretStr("")


class UploadConfig(Section):
    chunk_mb: int = Field(default=8, gt=0)
    parallel_files: int = Field(default=3, gt=0)
    full_fingerprint_max_mb: int = Field(default=200, gt=0)
    append_max_mb: int = Field(default=50, gt=0)


class JobsConfig(Section):
    workers: int = Field(default=4, ge=1, le=32)


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
    # 改名、修改标签这类不动文件内容的轻操作是否只需终审一级；默认仍走两级。
    light_ops_single_review: bool = False


class PurgeConfig(Section):
    cooling_days: int = Field(default=30, gt=0)


class ScheduleConfig(Section):
    inspection: str = "0 2 * * *"
    backup: str = "0 3 * * *"


class SearchConfig(Section):
    # auto：优先 sqlite-vec，加载失败自动改用 numpy 后备；也可强制指定其中一种。
    vector_backend: Literal["auto", "sqlite-vec", "numpy"] = "auto"
    vector_top_k: int = Field(default=50, gt=0)
    max_candidates: int = Field(default=60, gt=0)
    relax_below: int = Field(default=10, ge=0)


class PathsConfig(Section):
    data_dir: Path = Path("./data")
    dev_nas_dir: Path = Path("./devdata/nas")


class DevUser(Section):
    username: str = Field(min_length=1)
    password: SecretStr
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
    search: SearchConfig = Field(default_factory=SearchConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

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
    if settings.nas.dsm_ca_file is not None and not settings.nas.dsm_ca_file.is_absolute():
        settings.nas.dsm_ca_file = (path.parent / settings.nas.dsm_ca_file).resolve()
    return settings
