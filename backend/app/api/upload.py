"""批次路由仅解析参数、调用服务；内容经有界分片直接传 NAS。"""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.api.dependencies import client_host, require_role, require_login
from app.db.models import User
from app.schemas.auth import Role
from app.schemas.upload import BatchResponse, CreateBatchRequest, PrecheckRequest, PrecheckResponse, ResumeRequest
from app.services.auth.service import COOKIE_NAME

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


def upload_user(request: Request, id: int, x_upload_token: str | None = Header(default=None)) -> User:
    return request.app.state.upload.authorize(id, x_upload_token, request.cookies.get(COOKIE_NAME), client_host(request))


@router.post("/precheck", response_model=PrecheckResponse)
def precheck(body: PrecheckRequest, request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> PrecheckResponse:
    return request.app.state.upload.precheck(body)


@router.post("", response_model=BatchResponse)
def create(body: CreateBatchRequest, request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> BatchResponse:
    return request.app.state.upload.create(body, user, client_host(request))


@router.get("", response_model=list[BatchResponse])
def list_batches(request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> list[BatchResponse]:
    return request.app.state.upload.list(user)


@router.get("/{id}", response_model=BatchResponse)
def detail(id: int, request: Request, user: User = Depends(require_login)) -> BatchResponse:
    return request.app.state.upload.view(id, user)


@router.post("/{id}/retry-ai", response_model=BatchResponse)
def retry_ai(id: int, request: Request, user: User = Depends(require_login)) -> BatchResponse:
    return request.app.state.upload.retry_ai(id, user, client_host(request))


@router.get("/{id}/upload-state", response_model=BatchResponse)
def state(id: int, request: Request, user: User = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.state(id, user, client_host(request))


@router.put("/{id}/files/{file_id}/chunks/{index}", response_model=ChunkResponse)
async def chunk(id: int, file_id: int, index: int, request: Request, user: User = Depends(upload_user)) -> ChunkResponse:
    data = await request.app.state.upload.read_body(request.stream())
    result = await run_in_threadpool(request.app.state.upload.chunk, id, file_id, index, data, user, client_host(request))
    return ChunkResponse(**result)


@router.post("/{id}/files/{file_id}/complete", response_model=BatchResponse)
def complete(id: int, file_id: int, request: Request, user: User = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.complete(id, file_id, user, client_host(request))


@router.post("/{id}/pause", response_model=BatchResponse)
def pause(id: int, request: Request, user: User = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.pause(id, user, client_host(request))


@router.post("/{id}/cancel", response_model=BatchResponse)
def cancel(id: int, request: Request, user: User = Depends(upload_user)) -> BatchResponse:
    return request.app.state.upload.cancel(id, user, client_host(request))


@router.post("/{id}/confirm", response_model=BatchResponse)
def confirm(id: int, request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> BatchResponse:
    return request.app.state.upload.confirm(id, user, client_host(request))


@router.post("/{id}/resume", response_model=BatchResponse)
def resume(id: int, body: ResumeRequest, request: Request, user: User = Depends(require_role(Role.UPLOADER))) -> BatchResponse:
    return request.app.state.upload.resume(id, body, user, client_host(request))
