"""NAS 工作目录下的只读 HTTP 入口。"""
from contextlib import ExitStack
from typing import Annotated, Literal

from anyio import from_thread
from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.catalog import AssetResponse
from app.api.dependencies import client_host, require_login_context
from app.errors import AppError, error_response
from app.schemas.nas_browser import BrowseQuery, DirectoryPage, FileInfo
from app.services.auth.service import LoginContext
from app.services.catalog.media import NO_CACHE, RangeError, file_headers, parse_range, read_chunks
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
    image = request.app.state.nas_browser.preview(login, path, int(size))
    return Response(image, media_type="image/jpeg", headers=NO_CACHE)


@router.get("/download")
@router.head("/download")
def download(request: Request, login: Login, path: PathQuery) -> Response:
    browser: NasBrowser = request.app.state.nas_browser
    head = request.method == "HEAD"
    host = client_host(request)
    lease = ExitStack()
    audited_path = None
    try:
        item, stream = lease.enter_context(browser.open(login, path))
        audited_path = item.path
        # 原始文件没有已登记的强校验器，带 If-Range 的请求回退为完整传输。
        span = parse_range(request.headers.get("range") if not request.headers.get("if-range") else None, item.size)
        headers = file_headers(item.name, item.size, item.modified_at.timestamp(), span, inline=False)
        status = 206 if span else 200
        if head:
            lease.close()
            return Response(status_code=status, media_type="application/octet-stream", headers=headers)
        if span:
            stream.seek(span[0])
        browser.record(login, host, audited_path, "续传" if span else "开始")

        def delivered(sent: bool) -> None:
            browser.record(login, host, audited_path, "完成" if sent else "中断")

        return AssetResponse(read_chunks(stream, int(headers["Content-Length"])), lease, delivered,
                             status_code=status, media_type="application/octet-stream", headers=headers)
    except RangeError as error:
        lease.close()
        if not head:
            browser.record(login, host, audited_path, "范围无效")
        response = error_response(416, error.code, error.message)
        response.headers.update({**NO_CACHE, "Content-Range": f"bytes */{error.size}", "Accept-Ranges": "bytes"})
        return response
    except (NasError, AppError):
        lease.close()
        if not head:
            browser.record(login, host, audited_path, "拒绝")
        raise
    except BaseException:
        lease.close()
        raise
