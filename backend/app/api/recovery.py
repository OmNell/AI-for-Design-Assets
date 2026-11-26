"""管理员资产恢复与报告接口。"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import client_host, require_role
from app.db.models import User
from app.errors import AppError
from app.schemas.auth import Role

router = APIRouter(prefix="/api/admin/recovery")
admin = require_role(Role.ADMIN)


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False
    plan_id: int | None = Field(default=None, gt=0)
    backup_name: str | None = Field(default=None, max_length=64)


class RecoveryRun(BaseModel):
    id: int
    action: str
    status: str
    actor: str
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    progress: dict
    summary: dict[str, int] | None


class RecoveryOverview(BaseModel):
    catalog_state: str
    nas_state: str
    nas_checked_at: datetime | None
    projects: int
    products: int
    files: int
    read_only: bool
    last_backup_at: datetime | None
    can_backup: bool
    runs: list[RecoveryRun]


@router.get("", response_model=RecoveryOverview)
def overview(request: Request, actor: User = Depends(admin)) -> dict:
    return request.app.state.recovery_admin.overview(actor)


@router.get("/reports/{identifier}")
def report(identifier: int, request: Request, actor: User = Depends(admin)) -> dict:
    return request.app.state.recovery_admin.report(actor, identifier)


@router.post("/{action}", response_model=RecoveryRun, status_code=202)
def submit(action: Literal["scan", "detect", "apply", "stage"], request: Request, body: RecoveryAction | None = None,
           actor: User = Depends(admin)) -> dict:
    data = body or RecoveryAction()
    if action in ("apply", "stage") and not data.confirmed:
        raise AppError("recovery_confirmation_required", "请先确认恢复计划", 422)
    return request.app.state.recovery_admin.submit(actor, client_host(request), action,
                                                    plan_id=data.plan_id, backup_name=data.backup_name)
