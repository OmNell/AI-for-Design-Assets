"""入库后操作草稿和差异预览。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login_context
from app.schemas.postops import LightKind, OperationBody, OperationResponse, ProjectOperations
from app.schemas.review import ReviewResponse
from app.schemas.upload import BatchResponse, CreateBatchRequest, PrecheckRequest, PrecheckResponse
from app.services.auth.service import LoginContext

router = APIRouter(prefix="/api")


@router.get("/projects/{project_id}/operations", response_model=ProjectOperations)
def operations(project_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> ProjectOperations:
    return request.app.state.postops.options(project_id, login)


@router.get("/operations/voided-projects", response_model=list[ProjectOperations])
def voided(request: Request, login: LoginContext = Depends(require_login_context)) -> list[ProjectOperations]:
    return request.app.state.postops.voided(login)


@router.post("/projects/{project_id}/ops/supplement/precheck", response_model=PrecheckResponse)
def precheck(project_id: int, body: PrecheckRequest, request: Request,
             login: LoginContext = Depends(require_login_context)) -> PrecheckResponse:
    return request.app.state.postops.precheck(project_id, body, login)


@router.post("/projects/{project_id}/ops/supplement", response_model=BatchResponse)
def supplement(project_id: int, body: CreateBatchRequest, request: Request,
               login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.create(body, login, client_host(request), project_id=project_id)


@router.post("/projects/{project_id}/ops/{kind}", response_model=OperationResponse)
def create(project_id: int, kind: LightKind, body: OperationBody, request: Request,
           login: LoginContext = Depends(require_login_context)) -> OperationResponse:
    return request.app.state.postops.create(project_id, kind, body, login, client_host(request))


@router.get("/batches/{batch_id}/operation", response_model=OperationResponse)
def read(batch_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> OperationResponse:
    return request.app.state.postops.read(batch_id, login)


@router.put("/batches/{batch_id}/operation", response_model=OperationResponse)
def update(batch_id: int, body: OperationBody, request: Request,
           login: LoginContext = Depends(require_login_context)) -> OperationResponse:
    return request.app.state.postops.update(batch_id, body, login, client_host(request))


@router.get("/batches/{batch_id}/operation/preview", response_model=OperationResponse)
def preview(batch_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> OperationResponse:
    return request.app.state.postops.read(batch_id, login, preview=True)


@router.post("/batches/{batch_id}/operation/submit", response_model=ReviewResponse)
def submit(batch_id: int, body: OperationBody, request: Request,
           login: LoginContext = Depends(require_login_context)) -> ReviewResponse:
    return request.app.state.postops.submit(batch_id, body, login, client_host(request))
