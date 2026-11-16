"""搜索与向量索引管理路由。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login, require_role
from app.db.models import User
from app.schemas.auth import MessageResponse, Role
from app.schemas.search import ClickRequest, IndexStatus, SearchRequest, SearchResponse

router = APIRouter(prefix="/api")


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest, request: Request, user: User = Depends(require_login)) -> SearchResponse:
    return request.app.state.search.search(body, user)


@router.post("/search/click", response_model=MessageResponse)
def click(body: ClickRequest, request: Request, user: User = Depends(require_login)) -> MessageResponse:
    request.app.state.search.click(body.log_id, body.product_id, user)
    return MessageResponse(message="已记录")


@router.get("/admin/search-index", response_model=IndexStatus)
def index_status(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> IndexStatus:
    return request.app.state.search.status(actor)


@router.post("/admin/search-index/rebuild", response_model=IndexStatus)
def rebuild(request: Request, actor: User = Depends(require_role(Role.ADMIN))) -> IndexStatus:
    return request.app.state.search.rebuild(actor, client_host(request))
