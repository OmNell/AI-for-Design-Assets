"""AI 配置和自由文本字段管理，不在响应中包含凭据。"""
from __future__ import annotations

from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AiSettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str
    api_key: SecretStr | None = None
    model_vision: str
    model_text: str
    model_embed: str
    concurrency: int = Field(ge=1)
    timeout_s: int = Field(gt=0)
    retries: int = Field(ge=0)
    stall_alert_min: int = Field(gt=0)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("接口地址必须为不含凭据、查询参数的 HTTP(S) 地址")
        return value


class AiSettingsResponse(BaseModel):
    base_url: str
    api_key_set: bool
    model_vision: str
    model_text: str
    model_embed: str
    concurrency: int
    timeout_s: int
    retries: int
    stall_alert_min: int


class TagFieldItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    hint: str = ""
    sort_order: int = Field(ge=0)
    enabled: bool = True

    @field_validator("label")
    @classmethod
    def nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段名不能为空")
        return value


class TagFieldsResponse(BaseModel):
    version: int
    fields: list[TagFieldItem]


class TagFieldEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    hint: str
    sort_order: int = Field(ge=0)


class TagFieldsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    fields: list[TagFieldEdit]

    @field_validator("fields")
    @classmethod
    def unique_keys_and_order(cls, value: list[TagFieldEdit]) -> list[TagFieldEdit]:
        if len({field.key for field in value}) != len(value):
            raise ValueError("字段 key 不能重复")
        if len({field.sort_order for field in value}) != len(value):
            raise ValueError("字段显示顺序不能重复")
        return value


class AiModelTestResult(BaseModel):
    kind: str
    model: str
    success: bool
    error: str | None = None


class AiTestResponse(BaseModel):
    results: list[AiModelTestResult]
