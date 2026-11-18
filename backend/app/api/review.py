"""核对页路由：只解析参数并调用服务。"""
from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.api.dependencies import client_host, require_login, require_login_context, require_role
from app.db.models import User
from app.services.auth.service import LoginContext
from app.errors import AppError
from app.schemas.auth import Role
from app.schemas.review import (BulkRequest, OwnerRequest, OwnerResponse, ProductPatch, ProductUpdate, RelationsRequest,
                                ReviewResponse, SubmitRequest, TagValue)

router = APIRouter(prefix="/api")


@router.get("/batches/{id}/products", response_model=ReviewResponse)
def products(id: int, request: Request, user: User = Depends(require_login)) -> ReviewResponse:
    return request.app.state.review.read(id, user)


@router.patch("/batches/{id}/products/{pid}", response_model=ProductUpdate)
def patch(id: int, pid: int, body: ProductPatch, request: Request, user: User = Depends(require_login)) -> ProductUpdate:
    return request.app.state.review.patch(id, pid, body, user, client_host(request))


@router.post("/batches/{id}/products/bulk", response_model=ReviewResponse)
def bulk(id: int, body: BulkRequest, request: Request, user: User = Depends(require_login)) -> ReviewResponse:
    return request.app.state.review.bulk(id, body, user, client_host(request))


@router.get("/tag-values", response_model=list[TagValue])
def tag_values(request: Request, field: str = Query(min_length=1, max_length=64), q: str = Query(default="", max_length=500),
               batch_id: int | None = None, user: User = Depends(require_login)) -> list[TagValue]:
    return request.app.state.review.tag_values(field, q, batch_id, user)


@router.post("/batches/{id}/products/{pid}/retry-ai", response_model=ProductUpdate)
def retry_ai(id: int, pid: int, request: Request, login: LoginContext = Depends(require_login_context)) -> ProductUpdate:
    return request.app.state.review.retry_ai(id, pid, login, client_host(request))


@router.put("/batches/{id}/products/{pid}/relations", response_model=ProductUpdate)
def relations(id: int, pid: int, body: RelationsRequest, request: Request, user: User = Depends(require_login)) -> ProductUpdate:
    return request.app.state.review.relations(id, pid, body.item_ids, body.confirm_kind, user, client_host(request))


@router.post("/batches/{id}/products", response_model=ReviewResponse)
async def append(id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> ReviewResponse:
    service = request.app.state.review
    limit = service.append_limit()
    declared = request.headers.get("content-length", "")
    # 解析表单前先按声明长度拒绝，避免超大请求体落到本机临时文件。
    if not declared.isdigit():
        raise AppError("length_required", "请求缺少内容长度", 411)
    if int(declared) > limit + 64 * 1024:
        raise AppError("invalid_file_size", "图片超过追加主图的大小上限", 413)
    async with request.form(max_files=1, max_fields=4) as form:
        upload, kind, filename, mtime = form.get("file"), form.get("kind"), form.get("filename"), form.get("mtime")
        if not isinstance(upload, UploadFile) or not isinstance(kind, str):
            raise AppError("validation_error", "请选择图片并指定放入套装或单品", 422)
        if mtime is not None and not (isinstance(mtime, str) and mtime.isdigit() and len(mtime) <= 18):
            raise AppError("validation_error", "修改时间格式不正确", 422)
        name = filename if isinstance(filename, str) and filename.strip() else upload.filename or ""
        data = await upload.read(limit + 1)
    return await run_in_threadpool(service.append, id, kind, name, data, int(mtime) if isinstance(mtime, str) else None,
                                   login, client_host(request))


@router.delete("/batches/{id}/products/{pid}", response_model=ReviewResponse)
def remove(id: int, pid: int, request: Request, login: LoginContext = Depends(require_login_context)) -> ReviewResponse:
    return request.app.state.review.remove(id, pid, login, client_host(request))


@router.post("/batches/{id}/submit", response_model=ReviewResponse)
def submit(id: int, body: SubmitRequest, request: Request, login: LoginContext = Depends(require_login_context)) -> ReviewResponse:
    return request.app.state.review.submit(id, body.note, login, client_host(request))


@router.put("/admin/batches/{id}/owner", response_model=OwnerResponse)
def change_owner(id: int, body: OwnerRequest, request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> OwnerResponse:
    return request.app.state.review.change_owner(id, body.user_id, actor, client_host(request))
