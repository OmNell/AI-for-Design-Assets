"""仅当前管理员可操作 NAS 配置；凭据由请求体临时输入。"""
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_role
from app.db.models import User
from app.schemas.auth import Role
from app.schemas.nas_settings import NasApply, NasBrowse, NasCandidate, NasCheck, NasNodes, NasStatus, NasTest, NasValidation

router = APIRouter(prefix="/api/admin/nas")
Admin = Annotated[User, Depends(require_role(Role.ADMIN))]


@router.get("/config", response_model=NasStatus)
def config(request: Request, actor: Admin) -> NasStatus:
    return request.app.state.nas_settings.state()


@router.get("/status", response_model=NasStatus)
def status(request: Request, actor: Admin) -> NasStatus:
    return request.app.state.nas_settings.check_active()


@router.post("/test", response_model=list[NasCheck])
def connect(request: Request, actor: Admin, body: NasTest) -> list[NasCheck]:
    return request.app.state.nas_settings.test(actor, body, client_host(request))


@router.post("/browse", response_model=NasNodes)
def browse(request: Request, actor: Admin, body: NasBrowse) -> NasNodes:
    return request.app.state.nas_settings.browse(actor, body, client_host(request))


@router.post("/validate", response_model=NasValidation)
def validate(request: Request, actor: Admin, body: NasCandidate) -> NasValidation:
    return request.app.state.nas_settings.validate(actor, body, client_host(request))


@router.post("/apply", response_model=NasStatus)
def apply(request: Request, actor: Admin, body: NasApply) -> NasStatus:
    return request.app.state.nas_settings.apply(actor, body, client_host(request))
