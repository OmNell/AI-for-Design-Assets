"""登录与角色依赖；管理员不自动继承审核权限。"""
from collections.abc import Callable

from fastapi import Depends, Request

from app.db.models import User
from app.errors import AppError
from app.schemas.auth import Role
from app.services.auth.service import COOKIE_NAME, LoginContext


def client_host(request: Request) -> str:
    return request.client.host if request.client else "未知"


def require_login_context(request: Request) -> LoginContext:
    return request.app.state.auth.context(request.cookies.get(COOKIE_NAME), client_host(request))


def require_login(login: LoginContext = Depends(require_login_context)) -> User:
    return login.user


def require_role(*roles: Role) -> Callable[..., User]:
    def dependency(user: User = Depends(require_login)) -> User:
        effective = set(user.roles)
        if effective & {"first_reviewer", "final_reviewer"}:
            effective.add("uploader")
        if not effective.intersection(roles):
            raise AppError("forbidden", "当前账号没有此操作权限", 403)
        return user
    return dependency
