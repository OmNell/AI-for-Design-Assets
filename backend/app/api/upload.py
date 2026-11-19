"""批次路由仅解析参数、调用服务；内容经有界分片直接传 NAS。"""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.api.dependencies import client_host, require_role, require_login, require_login_context
from app.db.models import User
from app.errors import AppError
from app.schemas.auth import Role
from app.schemas.upload import BatchResponse, CreateBatchRequest, PrecheckRequest, PrecheckResponse, ResumeRequest, TerminateBatchRequest
from app.services.auth.service import COOKIE_NAME, LoginContext

router = APIRouter(prefix="/api/batches")


def cached_user(request: Request) -> User:
    return request.app.state.auth.current(request.cookies.get(COOKIE_NAME), client_host(request), require_nas=False)


@router.get("/{id}/thumbs/{batch_file_id}/{size}")
def thumbnail(id: int, batch_file_id: int, size: int, request: Request,
              user: User = Depends(cached_user)) -> Response:
    data = request.app.state.thumbs.image(id, batch_file_id, size, user)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


class ChunkResponse(BaseModel):
    bytes_received: int


def upload_user(request: Request, id: int, x_upload_token: str | None = Header(default=None)) -> LoginContext:
    return request.app.state.upload.authorize(id, x_upload_token, request.cookies.get(COOKIE_NAME), client_host(request))


@router.post("/precheck", response_model=PrecheckResponse)
def precheck(body: PrecheckRequest, request: Request, user: User = Depends(require_role(Role.UPLOADER)), resume_id: int | None = None) -> PrecheckResponse:
    if resume_id is not None:
        batch = request.app.state.upload.view(resume_id, user)
        if batch.uploader_id != user.id:
            raise AppError("forbidden", "只能核对本人的续传清单", 403)
    return request.app.state.upload.precheck(body, resume_id)


@router.post("", response_model=BatchResponse)
def create(body: CreateBatchRequest, request: Request, login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.create(body, login, client_host(request))


@router.get("", response_model=list[BatchResponse])
def list_batches(request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> list[BatchResponse]:
    return request.app.state.upload.list(user)


@router.get("/{id}", response_model=BatchResponse)
def detail(id: int, request: Request, user: User = Depends(require_login)) -> BatchResponse:
    return request.app.state.upload.view(id, user)


@router.post("/{id}/retry-ai", response_model=BatchResponse)
def retry_ai(id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.retry_ai(id, login, client_host(request))


@router.get("/{id}/upload-state", response_model=BatchResponse)
def state(id: int, request: Request, login: LoginContext = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.state(id, login, client_host(request))


@router.put("/{id}/files/{file_id}/chunks/{index}", response_model=ChunkResponse)
async def chunk(id: int, file_id: int, index: int, request: Request, login: LoginContext = Depends(upload_user)) -> ChunkResponse:
    limit = await run_in_threadpool(request.app.state.upload.body_limit, id, login)
    data = await request.app.state.upload.read_body(request.stream(), limit)
    result = await run_in_threadpool(request.app.state.upload.chunk, id, file_id, index, data, login, client_host(request))
    return ChunkResponse(**result)


@router.post("/{id}/files/{file_id}/complete", response_model=BatchResponse)
def complete(id: int, file_id: int, request: Request, login: LoginContext = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.complete(id, file_id, login, client_host(request))


@router.post("/{id}/pause", response_model=BatchResponse)
def pause(id: int, request: Request, login: LoginContext = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.pause(id, login, client_host(request))


@router.post("/{id}/cancel", response_model=BatchResponse)
def terminate(id: int, body: TerminateBatchRequest, request: Request, login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.terminate(id, login, body.reason, client_host(request), confirmed=body.confirmed)


@router.post("/{id}/confirm", response_model=BatchResponse)
def confirm(id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.confirm(id, login, client_host(request))


@router.post("/{id}/resume", response_model=BatchResponse)
def resume(id: int, body: ResumeRequest, request: Request, login: LoginContext = Depends(require_login_context)) -> BatchResponse:
    return request.app.state.upload.resume(id, body, login, client_host(request))
