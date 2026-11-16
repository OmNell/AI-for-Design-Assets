"""统一 API 错误结构；不把请求体、配置、异常原文写入响应。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.services.nas.errors import NasSessionExpired, NasPermissionDenied, NasUnavailable, NasNotFound

logger = logging.getLogger("nas_workbench")


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: object | None = None


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, detail: object | None = None) -> None:
        self.detail = detail
        self.code = code
        self.message = message
        self.status_code = status_code


def error_response(status: int, code: str, message: str, detail: object | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(code=code, message=message, detail=detail).model_dump())


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError) -> JSONResponse:
        response = error_response(error.status_code, error.code, error.message, error.detail)
        if error.status_code == 416 and isinstance(error.detail, dict) and "size" in error.detail:
            response.headers["Content-Range"] = f"bytes */{error.detail['size']}"
        if error.status_code == 401:
            response.delete_cookie("nas_workbench_session", path="/", httponly=True, samesite="lax")
        return response

    @app.exception_handler(NasSessionExpired)
    async def nas_expired(request: Request, error: NasSessionExpired) -> JSONResponse:
        response = error_response(401, "nas_session_expired", "NAS 连接已超时，请重新登录")
        response.delete_cookie("nas_workbench_session", path="/", httponly=True, samesite="lax")
        return response

    @app.exception_handler(NasPermissionDenied)
    async def nas_denied(request: Request, error: NasPermissionDenied) -> JSONResponse:
        return error_response(403, "nas_permission_denied", "NAS 无权限")

    @app.exception_handler(NasUnavailable)
    async def nas_unavailable(request: Request, error: NasUnavailable) -> JSONResponse:
        return error_response(503, "nas_unavailable", "NAS 暂时不可用，请重试或继续上传")

    @app.exception_handler(NasNotFound)
    async def nas_not_found(request: Request, error: NasNotFound) -> JSONResponse:
        return error_response(404, "nas_not_found", "NAS 文件或目录不存在")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        messages = {404: "请求的资源不存在", 405: "请求方法不支持"}
        return error_response(error.status_code, f"http_{error.status_code}", messages.get(error.status_code, "请求未能完成"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        detail = [{"field": ".".join(str(p) for p in item["loc"]), "message": "字段格式不正确"}
                  for item in error.errors()]
        return error_response(422, "validation_error", "请求参数不正确", detail)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.error("请求处理失败，已返回统一错误响应")
        return error_response(500, "internal_error", "服务暂时不可用")
