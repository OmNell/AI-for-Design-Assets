"""登录、注销与当前用户路由。"""
from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import client_host, require_login
from app.db.models import User
from app.schemas.auth import LoginRequest, MessageResponse, UserResponse
from app.services.auth.service import COOKIE_NAME

router = APIRouter(prefix="/api/auth")


@router.post("/login", response_model=UserResponse)
def login(body: LoginRequest, request: Request, response: Response) -> UserResponse:
    user, token, expires = request.app.state.auth.login(body.username, body.password.get_secret_value(), client_host(request))
    response.set_cookie(COOKIE_NAME, token, max_age=request.app.state.settings.server.session_hours * 3600,
                        expires=expires, path="/", httponly=True, secure=request.url.scheme == "https", samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user)


@router.get("/me", response_model=UserResponse)
def me(response: Response, user: User = Depends(require_login)) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user)


@router.post("/logout", response_model=MessageResponse)
def logout(request: Request, response: Response) -> MessageResponse:
    request.app.state.auth.logout(request.cookies.get(COOKIE_NAME, ""), client_host(request))
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return MessageResponse(message="已退出登录")
