"""项目、商品、文件与永久链接的 HTTP 入口。"""
from __future__ import annotations

from contextlib import ExitStack
import re
from typing import Annotated, Callable, Iterator

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.dependencies import client_host, require_login_context
from app.errors import AppError, error_response
from app.schemas.catalog import (AssetFinding, DownloadItem, DownloadManyRequest, DownloadProgress, FileCheck, FileRow, ProductDetail,
                                 ProjectDetail, ProjectFilters, ProjectPage)
from app.schemas.ingest import ResolvedLink
from app.services.auth.service import LoginContext
from app.services.catalog.media import MediaService, NO_CACHE, RangeError, is_image, parse_range, public_error, stream_headers, transfer_chunks
from app.services.nas import NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable

router = APIRouter()
Login = Annotated[LoginContext, Depends(require_login_context)]
Transfer = Annotated[str | None, Query(min_length=12, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]


@router.get("/api/projects", response_model=ProjectPage)
def projects(request: Request, filters: Annotated[ProjectFilters, Query()], login: Login) -> ProjectPage:
    return request.app.state.catalog.list(filters, login, client_host(request))


@router.get("/api/projects/{project_id}", response_model=ProjectDetail)
def project(project_id: int, request: Request, login: Login) -> ProjectDetail:
    return request.app.state.catalog.project(project_id, login, client_host(request))


@router.get("/api/projects/{project_id}/files", response_model=list[FileRow])
def files(project_id: int, request: Request, login: Login, category: str | None = None) -> list[FileRow]:
    return request.app.state.catalog.files(project_id, category, login, client_host(request))


@router.get("/api/products/{product_id}", response_model=ProductDetail)
def product(product_id: int, request: Request, login: Login) -> ProductDetail:
    return request.app.state.catalog.product(product_id, login, client_host(request))


@router.get("/api/files/{file_id}/check", response_model=FileCheck)
def check(file_id: int, request: Request, login: Login) -> FileCheck:
    return request.app.state.catalog.media.check(file_id, login, client_host(request))


@router.post("/api/files/download-many", response_model=list[DownloadItem])
def many(body: DownloadManyRequest, request: Request, login: Login) -> list[DownloadItem]:
    return request.app.state.catalog.media.many(body.file_ids, login, client_host(request))


@router.get("/api/admin/asset-findings", response_model=list[AssetFinding])
def findings(request: Request, login: Login) -> list[AssetFinding]:
    return request.app.state.catalog.findings(login)


@router.get("/api/thumbs/{product_id}/{size}")
def thumbnail(product_id: int, size: int, request: Request, login: Login, transfer: Transfer = None) -> Response:
    media = request.app.state.catalog.media
    picture, headers = media.preview(media.main_image_id(product_id), size, login, client_host(request),
                                     product_id=product_id, transfer=transfer)
    return Response(picture, media_type="image/jpeg", headers=headers)


@router.get("/api/files/{file_id}/preview")
def preview(file_id: int, request: Request, login: Login, size: int = 1600, allow_changed: bool = False,
            original: bool = False, transfer: Transfer = None) -> Response:
    if original:
        return _stream(file_id, request, login, allow_changed, transfer, inline=True)
    picture, headers = request.app.state.catalog.media.preview(file_id, size, login, client_host(request),
                                                               allow_changed=allow_changed, transfer=transfer)
    return Response(picture, media_type="image/jpeg", headers=headers)


class AssetResponse(StreamingResponse):
    """HTTP 发送结束或断开时，释放进入响应前已打开的文件和用户会话。"""
    def __init__(self, chunks: Iterator[bytes], lease: ExitStack,
                 delivery: Callable[[bool], None] | None = None, **kwargs: object) -> None:
        super().__init__(chunks, **kwargs)
        self.lease = lease
        self.delivery = delivery
        self.sent = False

    async def stream_response(self, send: Send) -> None:
        await super().stream_response(send)
        self.sent = True

    def close(self) -> None:
        try:
            self.lease.close()
        finally:
            if self.delivery:
                self.delivery(self.sent)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.close)


def _stream(file_id: int, request: Request, login: LoginContext, allow_changed: bool, transfer: str | None,
            *, inline: bool = False) -> Response:
    lease = ExitStack()
    media: MediaService = request.app.state.catalog.media
    host = client_host(request)
    kind = "preview" if inline else "download"
    requested = request.headers.get("range")
    is_head = request.method == "HEAD"
    try:
        asset = lease.enter_context(media.open(file_id, login, host))
        if asset.warning and not allow_changed:
            raise AppError("content_changed", asset.warning, 409)
        content_type = "application/octet-stream"
        if inline:
            if not is_image(asset.name):
                raise AppError("preview_unsupported", "该格式请下载后查看", 415)
            magic = asset.stream.read(8)
            if magic.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif magic == b"\x89PNG\r\n\x1a\n":
                content_type = "image/png"
            else:
                raise AppError("preview_unsupported", "该格式请下载后查看", 415)
        base = stream_headers(asset, None, inline=inline)
        # 漂移文件仅使用内容 ETag 做 If-Range，mtime 可能被保留。
        validator = request.headers.get("if-range")
        valid = {base["ETag"]} if asset.warning else {base["ETag"], base["Last-Modified"]}
        span = parse_range(requested, asset.size) if not validator or validator in valid else None
        offset = span[0] if span else 0
        asset.stream.seek(offset)
        headers = stream_headers(asset, span, inline=inline)
        status = 206 if span else 200
        if is_head:
            lease.close()
            return Response(status_code=status, media_type=content_type, headers=headers)
        finishing = span is None or span[1] == asset.size - 1
        media.record(login, kind, file_id, host, "warning" if asset.warning else "allowed", transfer,
                     resume=requested is not None, track=finishing)

        def body():
            try:
                yield from transfer_chunks(asset, int(headers["Content-Length"]))
            except (NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable):
                media.interrupted(login, file_id, host)
                raise OSError("文件传输中断，可重新登录或续传") from None

        delivery = None
        if transfer and not inline and finishing:
            delivery = lambda sent: media.delivered(login, file_id, transfer, sent)
        return AssetResponse(body(), lease, delivery, status_code=status, media_type=content_type, headers=headers)
    except RangeError as error:
        lease.close()
        if not is_head and transfer:
            media.record(login, kind, file_id, host, error.code, transfer)
        response = error_response(416, error.code, error.message)
        response.headers.update({**NO_CACHE, "Content-Range": f"bytes */{error.size}", "Accept-Ranges": "bytes"})
        return response
    except (AppError, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable) as error:
        lease.close()
        if not is_head:
            media.record(login, kind, file_id, host, public_error(error)[0], transfer, resume=requested is not None)
        raise
    except BaseException:
        lease.close()
        raise


@router.get("/api/files/{file_id}/download")
@router.head("/api/files/{file_id}/download")
def download(file_id: int, request: Request, login: Login, allow_changed: bool = False,
             transfer: Transfer = None) -> Response:
    return _stream(file_id, request, login, allow_changed, transfer)


@router.get("/api/files/{file_id}/downloads/{transfer}", response_model=DownloadProgress)
def delivery(file_id: int, transfer: str, request: Request, response: Response, login: Login) -> DownloadProgress:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{12,64}", transfer):
        raise AppError("invalid_transfer", "下载记录标识不正确", 422)
    response.headers.update(NO_CACHE)
    return request.app.state.catalog.media.delivery(login, file_id, transfer)


@router.get("/api/l/{link_id}", response_model=ResolvedLink)
def resolve(link_id: str, request: Request, login: Login) -> ResolvedLink:
    return request.app.state.links.resolve(link_id, login, client_host(request))


@router.get("/l/{link_id}", response_model=ResolvedLink)
def permanent(link_id: str, request: Request) -> Response:
    if re.search(r"\btext/html\b", request.headers.get("accept", "")) or request.headers.get("sec-fetch-mode") == "navigate":
        index = request.app.state.static_index
        if not index.is_file():
            raise AppError("web_unavailable", "页面尚未构建", 404)
        return FileResponse(index, headers={"Cache-Control": "no-cache", "Vary": "Accept, Sec-Fetch-Mode"})
    login = require_login_context(request)
    result = resolve(link_id, request, login)
    return Response(result.model_dump_json(), media_type="application/json",
                    headers={**NO_CACHE, "Vary": "Accept, Sec-Fetch-Mode"})
