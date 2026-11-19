"""入库后操作的执行进度及终审恢复。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login_context
from app.schemas.postop_execution import ExecuteInput, OperationExecution
from app.services.auth.service import LoginContext

router = APIRouter()


@router.get("/api/operations/{batch_id}/execution", response_model=OperationExecution)
def progress(batch_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> OperationExecution:
    return request.app.state.postop_executor.read(batch_id, login)


@router.post("/api/operations/{batch_id}/execute", response_model=OperationExecution)
def execute(batch_id: int, data: ExecuteInput, request: Request,
            login: LoginContext = Depends(require_login_context)) -> OperationExecution:
    return request.app.state.postop_executor.resume(batch_id, data.confirm_code, login, client_host(request))
