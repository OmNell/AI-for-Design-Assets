"""双模式检索、点击及管理员向量索引入口。"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import client_host, require_login_context
from app.schemas.search import BuildIndex, ClickRequest, IndexStatus, SearchRequest, SearchResult
from app.services.auth.service import LoginContext

router = APIRouter(prefix="/api")
Login = Annotated[LoginContext, Depends(require_login_context)]


@router.post("/search", response_model=SearchResult)
def search(body: SearchRequest, request: Request, login: Login) -> SearchResult:
    return request.app.state.search.search(body, login, client_host(request))


@router.post("/search/click", status_code=204)
def click(body: ClickRequest, request: Request, login: Login) -> Response:
    request.app.state.search.click(body, login, client_host(request))
    return Response(status_code=204)


@router.get("/admin/vector-indexes", response_model=IndexStatus)
def indexes(request: Request, login: Login) -> IndexStatus:
    return request.app.state.indexes.read(login)


@router.post("/admin/vector-indexes", response_model=IndexStatus, status_code=202)
def build(body: BuildIndex, request: Request, login: Login) -> IndexStatus:
    return request.app.state.indexes.create(body, login, client_host(request))


@router.post("/admin/vector-indexes/{generation}/{action}", response_model=IndexStatus)
def index_action(generation: int, action: Literal["retry", "switch", "rollback", "cancel"],
                 request: Request, login: Login) -> IndexStatus:
    return request.app.state.indexes.action(generation, action, login, client_host(request))
