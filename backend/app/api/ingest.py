"""入库进度、继续入库及永久链接的 JSON 解析入口。"""
from fastapi import APIRouter, Depends, Request

from app.api.dependencies import client_host, require_login_context
from app.schemas.ingest import IngestProgress, ResolvedLink
from app.services.auth.service import LoginContext

router = APIRouter()


@router.get("/api/review/{batch_id}/ingest", response_model=IngestProgress | None)
def progress(batch_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> IngestProgress | None:
    return request.app.state.ingest.read(batch_id, login)


@router.post("/api/review/{batch_id}/resume-ingest", response_model=IngestProgress)
def resume(batch_id: int, request: Request, login: LoginContext = Depends(require_login_context)) -> IngestProgress:
    return request.app.state.ingest.resume(batch_id, login, client_host(request))


@router.get("/l/{link_id}", response_model=ResolvedLink)
def resolve(link_id: str, request: Request, login: LoginContext = Depends(require_login_context)) -> ResolvedLink:
    return request.app.state.links.resolve(link_id, login, client_host(request))
