"""只读运行健康检查。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import __version__
from app.errors import AppError, ErrorResponse

router = APIRouter(prefix="/api")


class HealthResponse(BaseModel):
    version: str
    mode: str
    database_available: bool


@router.get("/health", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
def health(request: Request) -> HealthResponse:
    if not request.app.state.database.is_available():
        raise AppError("database_unavailable", "数据库暂时不可用", 503)
    config = request.app.state.settings
    return HealthResponse(version=__version__, mode=config.mode, database_available=True)
