"""审核、对照表、目录树与待办路由。"""
from typing import Literal

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login, require_role
from app.db.models import User
from app.schemas.audit import (AuditResponse, CompanyMap, PassRequest, PathRequest, PinsRequest, QueueItem, RejectRequest,
                               TodoItem, TreeRoot)
from app.schemas.auth import Role

router = APIRouter(prefix="/api")


@router.get("/admin/company-map", response_model=CompanyMap)
def company_map(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> CompanyMap:
    return request.app.state.company_map.read(actor)


@router.put("/admin/company-map", response_model=CompanyMap)
def save_company_map(body: CompanyMap, request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> CompanyMap:
    return request.app.state.company_map.save(actor, body, client_host(request))


@router.get("/catalog/tree", response_model=list[TreeRoot])
def catalog_tree(request: Request, user: User = Depends(require_login)) -> list[TreeRoot]:
    return request.app.state.company_map.tree(user)


@router.get("/todos", response_model=list[TodoItem])
def todos(request: Request, user: User = Depends(require_login)) -> list[TodoItem]:
    return request.app.state.notify.todos(user)


@router.get("/review/queue", response_model=list[QueueItem])
def queue(stage: Literal["first", "final"], request: Request, user: User = Depends(require_login)) -> list[QueueItem]:
    return request.app.state.audit.queue(stage, user)


@router.get("/review/{batch_id}", response_model=AuditResponse)
def read(batch_id: int, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.read(batch_id, user)


@router.post("/review/{batch_id}/claim", response_model=AuditResponse)
def claim(batch_id: int, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.claim(batch_id, user, client_host(request))


@router.post("/review/{batch_id}/release", response_model=AuditResponse)
def release(batch_id: int, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.release(batch_id, user, client_host(request))


@router.put("/review/{batch_id}/pins", response_model=AuditResponse)
def pins(batch_id: int, body: PinsRequest, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.pins(batch_id, body.pins, user, client_host(request))


@router.put("/review/{batch_id}/path", response_model=AuditResponse)
def change_path(batch_id: int, body: PathRequest, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.change_path(batch_id, body.won, body.company_id, user, client_host(request))


@router.post("/review/{batch_id}/pass", response_model=AuditResponse)
def approve(batch_id: int, body: PassRequest, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.approve(batch_id, body.checklist, user, client_host(request))


@router.post("/review/{batch_id}/reject", response_model=AuditResponse)
def reject(batch_id: int, body: RejectRequest, request: Request, user: User = Depends(require_login)) -> AuditResponse:
    return request.app.state.audit.reject(batch_id, body.comment, user, client_host(request))
