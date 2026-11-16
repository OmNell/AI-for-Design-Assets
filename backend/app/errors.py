"""统一 API 错误结构；不把请求体、配置、异常原文写入响应。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

logger = logging.getLogger("nas_workbench")


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: object | None = None


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code


def error_response(status: int, code: str, message: str, detail: object | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(code=code, message=message, detail=detail).model_dump())


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError) -> JSONResponse:
        return error_response(error.status_code, error.code, error.message)

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
