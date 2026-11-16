"""管理员角色表 API。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_role
from app.db.models import User
from app.schemas.auth import Role, RoleUpdate, UserResponse

router = APIRouter(prefix="/api/admin")


@router.get("/roles", response_model=list[UserResponse])
def list_roles(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> list[UserResponse]:
    return [UserResponse.model_validate(user) for user in request.app.state.roles.list_users(actor)]


@router.put("/roles", response_model=UserResponse)
def update_roles(body: RoleUpdate, request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> UserResponse:
    return UserResponse.model_validate(request.app.state.roles.update(actor, body, client_host(request)))
