"""AI 设置和字段管理路由，仅做权限依赖和数据转换。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_role
from app.db.models import User
from app.schemas.ai import AiSettingsResponse, AiSettingsWrite, TagFieldsResponse, TagFieldsWrite
from app.schemas.auth import Role

router = APIRouter()


@router.get("/api/settings/ai", response_model=AiSettingsResponse)
def read_ai(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> AiSettingsResponse:
    return request.app.state.ai_settings.read(actor)


@router.put("/api/settings/ai", response_model=AiSettingsResponse)
def save_ai(body: AiSettingsWrite, request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> AiSettingsResponse:
    return request.app.state.ai_settings.save(actor, body, client_host(request))


@router.get("/api/admin/tag-fields", response_model=TagFieldsResponse)
def read_fields(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> TagFieldsResponse:
    return request.app.state.tag_fields.read(actor)


@router.put("/api/admin/tag-fields", response_model=TagFieldsResponse)
def save_fields(body: TagFieldsWrite, request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> TagFieldsResponse:
    return request.app.state.tag_fields.save(actor, body, client_host(request))
