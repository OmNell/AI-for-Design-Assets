"""管理员 NAS 接入设置。输入与公开视图区分，凭据不进入响应。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class NasTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    host: str = Field(min_length=1, max_length=253)
    dsm_port: int = Field(default=5001, ge=1, le=65535)
    share: str = Field(min_length=1, max_length=80)


class NasCandidate(NasTarget):
    work_root: str = Field(max_length=768)
    system_user: str = Field(min_length=1, max_length=128)
    system_password: SecretStr | None = Field(default=None, max_length=512)
    actor_password: SecretStr | None = Field(default=None, max_length=512)


class NasBrowse(NasTarget):
    path: str = Field(default="", max_length=768)
    actor_password: SecretStr = Field(min_length=1, max_length=512)


class NasTest(NasTarget):
    actor_password: SecretStr = Field(min_length=1, max_length=512)
    protocol: Literal["dsm", "smb"]
    permission_profile: Literal["employee", "final"] | None = None
    work_root: str = Field(default="", max_length=768)
    username: str | None = Field(default=None, min_length=1, max_length=128)


class NasApply(NasCandidate):
    revision: str = Field(min_length=64, max_length=64)
    confirmed: Literal[True]


class NasCheck(BaseModel):
    name: str
    state: Literal["passed", "failed", "manual"]
    message: str


class NasPublic(NasTarget):
    work_root: str | None
    system_user: str
    has_system_password: bool
    certificate: Literal["trusted_ca", "system_ca", "legacy_http", "mac_test_disabled"]
    regions: dict[str, str]


class NasStatus(BaseModel):
    state: Literal["active", "pending_restart", "failed"]
    revision: str | None
    active: NasPublic
    pending: NasPublic | None = None
    message: str
    mode: Literal["dev", "prod"]
    connection_checks: list[NasCheck] = Field(default_factory=list)


class NasValidation(BaseModel):
    eligible: bool
    checks: list[NasCheck]
    changes: list[str]
    conflicts: list[str]
    regions: dict[str, str]


class NasNodes(BaseModel):
    path: str
    nodes: list[str]
    truncated: bool
