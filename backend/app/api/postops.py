"""入库后操作路由（7.5）。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login, require_role
from app.db.models import User
from app.schemas.auth import Role
from app.schemas.postops import (MoveRequest, OpDetail, PurgePreview, PurgeRequest, PurgeTargets, ReasonRequest, RenameRequest,
                                 RestoreRequest, VoidRequest)

router = APIRouter(prefix="/api")
initiator = require_role(Role.UPLOADER)
final = require_role(Role.FINAL_REVIEWER)


@router.post("/projects/{id}/ops/rename", response_model=OpDetail)
def rename(id: int, body: RenameRequest, request: Request, user: User = Depends(initiator)) -> OpDetail:
    return request.app.state.postops.rename(id, body, user, client_host(request))


@router.post("/projects/{id}/ops/retag", response_model=OpDetail)
def retag(id: int, request: Request, user: User = Depends(initiator)) -> OpDetail:
    return request.app.state.postops.retag(id, user, client_host(request))


@router.post("/projects/{id}/ops/void", response_model=OpDetail)
def void(id: int, body: VoidRequest, request: Request, user: User = Depends(initiator)) -> OpDetail:
    return request.app.state.postops.void(id, body, user, client_host(request))


@router.post("/projects/{id}/ops/void-project", response_model=OpDetail)
def void_project(id: int, body: ReasonRequest, request: Request, user: User = Depends(final)) -> OpDetail:
    return request.app.state.postops.void_project(id, body.reason, user, client_host(request))


@router.post("/projects/{id}/ops/restore", response_model=OpDetail)
def restore(id: int, body: RestoreRequest, request: Request, user: User = Depends(final)) -> OpDetail:
    return request.app.state.postops.restore(id, body, user, client_host(request))


@router.post("/projects/{id}/ops/move", response_model=OpDetail)
def move(id: int, body: MoveRequest, request: Request, user: User = Depends(final)) -> OpDetail:
    return request.app.state.postops.move(id, body, user, client_host(request))


@router.post("/projects/{id}/ops/purge/preview", response_model=PurgePreview)
def purge_preview(id: int, body: PurgeTargets, request: Request, user: User = Depends(final)) -> PurgePreview:
    return request.app.state.postops.purge_preview(id, body, user)


@router.post("/projects/{id}/ops/purge", response_model=OpDetail)
def purge(id: int, body: PurgeRequest, request: Request, user: User = Depends(final)) -> OpDetail:
    return request.app.state.postops.purge(id, body, user, client_host(request))


@router.get("/ops/{batch_id}", response_model=OpDetail)
def detail(batch_id: int, request: Request, user: User = Depends(require_login)) -> OpDetail:
    return request.app.state.postops.detail(batch_id, user)


@router.post("/ops/{batch_id}/withdraw", response_model=OpDetail)
def withdraw(batch_id: int, request: Request, user: User = Depends(require_login)) -> OpDetail:
    return request.app.state.postops.withdraw(batch_id, user, client_host(request))
