"""AI 连接测试及停滞异常查询。"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import client_host, require_role
from app.db.models import User
from app.schemas.ai import AiTestResponse
from app.schemas.auth import Role

router = APIRouter(prefix="/api")


class AiAlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: int
    run_id: str
    reason: str
    detected_at: datetime


@router.post("/settings/ai/test", response_model=AiTestResponse)
def test_connection(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> AiTestResponse:
    return AiTestResponse(results=request.app.state.ai.test_models(actor, client_host(request)))


@router.get("/admin/ai-alerts", response_model=list[AiAlertResponse])
def alerts(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> list[AiAlertResponse]:
    return [AiAlertResponse.model_validate(row) for row in request.app.state.ai.alerts(actor)]
