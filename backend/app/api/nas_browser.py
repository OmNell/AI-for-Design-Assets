"""NAS 工作目录下的只读 HTTP 入口。"""
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Annotated, Literal

from anyio import from_thread
from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.catalog import AssetResponse, Transfer
from app.api.dependencies import client_host, require_login_context
from app.errors import AppError, error_response
from app.schemas.nas_browser import BrowseQuery, DirectoryPage, FileInfo
from app.services.auth.service import LoginContext
from app.services.catalog.media import NO_CACHE, RangeError, file_headers, parse_range, public_error, read_chunks
from app.services.nas.errors import NasError, NasUnavailable
from app.services.nas_browser import NasBrowser

router = APIRouter(prefix="/api/nas-browser")
Login = Annotated[LoginContext, Depends(require_login_context)]
PathQuery = Annotated[str, Query(max_length=768)]


@router.get("/list", response_model=DirectoryPage)
def list_directory(request: Request, response: Response, login: Login, query: Annotated[BrowseQuery, Query()]) -> DirectoryPage:
    response.headers.update(NO_CACHE)
    try:
        return request.app.state.nas_browser.list(login, query, lambda: from_thread.run(request.is_disconnected))
    except NasUnavailable:
        raise AppError("nas_unavailable", "NAS 目录暂时不可用，请刷新重试", 503) from None


@router.get("/info", response_model=FileInfo)
def info(request: Request, response: Response, login: Login, path: PathQuery = "") -> FileInfo:
    response.headers.update(NO_CACHE)
    return request.app.state.nas_browser.info(login, path)


@router.get("/preview", responses={200: {"content": {"image/jpeg": {}}}})
def preview(request: Request, login: Login, path: PathQuery, size: Literal["300", "1600"] = "1600") -> Response:
    browser: NasBrowser = request.app.state.nas_browser
    host = client_host(request)
    try:
        item, image = browser.preview(login, path, int(size))
    except (NasError, AppError) as error:
        if size == "1600":
            browser.audit.record(login, "nas_preview", host, None, public_error(error)[0])
        raise
    if size == "1600":
        browser.audit.record(login, "nas_preview", host, item.path, "allowed")
    return Response(image, media_type="image/jpeg", headers=NO_CACHE)


@router.get("/download")
@router.head("/download")
def download(request: Request, login: Login, path: PathQuery, transfer: Transfer = None) -> Response:
    browser: NasBrowser = request.app.state.nas_browser
    head = request.method == "HEAD"
    host = client_host(request)
    lease = ExitStack()
    audited_path = None
    try:
        item, stream = lease.enter_context(browser.open(login, path))
        audited_path = item.path
        # 原始文件没有已登记的强校验器，带 If-Range 的请求回退为完整传输。
        requested = request.headers.get("range")
        span = parse_range(requested if not request.headers.get("if-range") else None, item.size)
        headers = file_headers(item.name, item.size, item.modified_at.timestamp(), span, inline=False)
        status = 206 if span else 200
        if head:
            lease.close()
            return Response(status_code=status, media_type="application/octet-stream", headers=headers)
        offset = span[0] if span else 0
        if span:
            stream.seek(offset)
        record_id = browser.audit.start(login, item, host, transfer, resume=span is not None)

        def delivered(sent: bool) -> None:
            browser.audit.delivered(record_id, item.size, offset, response.sent_bytes, sent)

        def body() -> Iterator[bytes]:
            try:
                yield from read_chunks(stream, int(headers["Content-Length"]))
            except NasError:
                raise OSError("NAS 文件传输中断，可重新登录或续传") from None

        response = AssetResponse(body(), lease, delivered,
                                 status_code=status, media_type="application/octet-stream", headers=headers)
        return response
    except RangeError as error:
        lease.close()
        if not head:
            browser.audit.record(login, "nas_download", host, audited_path, error.code, transfer)
        response = error_response(416, error.code, error.message)
        response.headers.update({**NO_CACHE, "Content-Range": f"bytes */{error.size}", "Accept-Ranges": "bytes"})
        return response
    except (NasError, AppError) as error:
        lease.close()
        if not head:
            browser.audit.record(login, "nas_download", host, audited_path, public_error(error)[0], transfer)
        raise
    except BaseException:
        lease.close()
        raise
