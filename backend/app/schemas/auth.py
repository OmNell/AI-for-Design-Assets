"""认证和角色管理的请求/响应，不在响应中暴露令牌或凭据。"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator


class Role(StrEnum):
    UPLOADER = "uploader"
    FIRST_REVIEWER = "first_reviewer"
    FINAL_REVIEWER = "final_reviewer"
    ADMIN = "admin"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: str | None
    roles: list[Role]
    enabled: bool


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    user_id: int = Field(gt=0)
    roles: list[Role]
    enabled: StrictBool

    @field_validator("roles")
    @classmethod
    def distinct_roles(cls, roles: list[Role]) -> list[Role]:
        if len(set(roles)) != len(roles):
            raise ValueError("角色不能重复")
        return roles


class MessageResponse(BaseModel):
    message: str
