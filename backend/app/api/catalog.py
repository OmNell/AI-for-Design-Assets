"""项目、商品、下载与永久链接路由。"""
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from app.api.dependencies import client_host, require_login
from app.api.upload import cached_user
from app.db.models import User
from app.schemas.catalog import DownloadItem, DownloadManyRequest, FileCheck, FileRow, LinkTarget, ProductDetail, ProjectDetail, ProjectPage
from app.services.catalog import content_disposition

router = APIRouter(prefix="/api")


@router.get("/projects", response_model=ProjectPage)
def projects(request: Request, q: str = Query(default="", max_length=200), won: bool | None = None, industry: int | None = None,
             company: int | None = None, page: int = Query(default=1, ge=1, le=100000), user: User = Depends(require_login)) -> ProjectPage:
    return request.app.state.catalog.projects(q, won, industry, company, page)


@router.get("/projects/{id}", response_model=ProjectDetail)
def project(id: int, request: Request, user: User = Depends(require_login)) -> ProjectDetail:
    return request.app.state.catalog.project(id)


@router.get("/projects/{id}/files", response_model=list[FileRow])
def project_files(id: int, request: Request, category: str | None = None, user: User = Depends(require_login)) -> list[FileRow]:
    return request.app.state.catalog.files(id, category)


@router.get("/products/{id}", response_model=ProductDetail)
def product(id: int, request: Request, user: User = Depends(require_login)) -> ProductDetail:
    return request.app.state.catalog.product(id)


@router.get("/thumbs/{product_id}/{size}")
def thumbnail(product_id: int, size: int, request: Request, user: User = Depends(cached_user)) -> Response:
    data, is_placeholder = request.app.state.catalog.thumbnail(product_id, size, user)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                                            "X-Thumbnail-Placeholder": "1" if is_placeholder else "0"})


@router.get("/files/{file_id}/check", response_model=FileCheck)
def check(file_id: int, request: Request, user: User = Depends(require_login)) -> FileCheck:
    return request.app.state.catalog.check(file_id, user, client_host(request))


@router.get("/files/{file_id}/download")
def download(file_id: int, request: Request, range: str | None = Header(default=None), if_range: str | None = Header(default=None),
             user: User = Depends(require_login)) -> StreamingResponse:
    item = request.app.state.catalog.download(file_id, range, if_range, user, client_host(request))
    headers = {"Content-Length": str(item.end - item.start + 1), "Accept-Ranges": "bytes", "Content-Disposition": content_disposition(item.name),
               "ETag": item.etag, "Last-Modified": item.modified, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if item.partial:
        headers["Content-Range"] = f"bytes {item.start}-{item.end}/{item.size}"
    return StreamingResponse(item.chunks(), status_code=206 if item.partial else 200, media_type="application/octet-stream", headers=headers)


@router.post("/files/download-many", response_model=list[DownloadItem])
def download_many(body: DownloadManyRequest, request: Request, user: User = Depends(require_login)) -> list[DownloadItem]:
    return request.app.state.catalog.download_many(body.file_ids)


@router.get("/l/{link_id}", response_model=LinkTarget)
def link(link_id: str, request: Request, format: str | None = None, user: User = Depends(require_login)) -> Response:
    """默认按目标重定向；带 format=json 时返回解析结果，供页面自行跳转或显示提示页。"""
    target = request.app.state.catalog.link(link_id)
    if format == "json":
        return JSONResponse(target.model_dump())
    if target.url is None:
        return JSONResponse(target.model_dump(), status_code=410)
    return RedirectResponse(target.url, status_code=307)
