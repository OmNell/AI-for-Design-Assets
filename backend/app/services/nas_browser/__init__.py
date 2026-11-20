"""基于当前登录设备会话提供目录、内容和下载审计。"""
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from threading import BoundedSemaphore
from typing import BinaryIO, Iterator

from sqlalchemy import func, select

from app.db.models import CatalogFile, Project, User
from app.errors import AppError
from app.schemas.nas_browser import BrowseQuery, DirectoryPage, FileEntry, FileInfo
from app.services.auth.service import AuthService, LoginContext
from app.services.catalog.media import is_image
from app.services.nas.fs import NasStat
from app.services.nas.paths import safe_join
from app.services.oplog import OplogService
from app.services.thumbs.images import ImageDecodeError, render_preview
from app.services.upload.service import UploadService

from app.services.nas.browser import ReadRoot

PAGE_SIZE = 60
PREVIEW_MAX_BYTES = 48 * 1024 * 1024


def entry(path: str, name: str, stat: NasStat) -> FileEntry:
    kind = "folder" if stat.is_dir else "image" if is_image(name) else "file"
    return FileEntry(path=path, name=name, kind=kind, is_dir=stat.is_dir, size=stat.size,
                     extension="" if stat.is_dir else PurePosixPath(name).suffix.lower().lstrip("."),
                     modified_at=datetime.fromtimestamp(stat.mtime, timezone.utc))


class NasBrowser:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.preview_slots = BoundedSemaphore(2)

    def root(self, login: LoginContext) -> ReadRoot:
        with self.auth.lock, self.auth.database.session() as session:
            UploadService._check_login(session, login)
            user = session.get(User, login.user.id)
            if user is None or not user.enabled:
                raise AppError("login_required", "账号已停用，请重新登录", 401)
            root = self.auth.config.nas.work_root
            if not root:
                raise AppError("nas_work_root_required", "请联系管理员先配置 NAS 工作目录", 409)
            return ReadRoot(self.auth.pool.get(login.session_id), root)

    def list(self, login: LoginContext, query: BrowseQuery, cancelled: Callable[[], bool]) -> DirectoryPage:
        root = self.root(login)
        rows = root.list(query.path, query.kind == "folder", cancelled)
        term = query.q.casefold()
        rows.sort(key=lambda row: (not row.stat.is_dir,
                  -row.stat.size if query.sort == "size" else -row.stat.mtime if query.sort == "modified" else 0,
                  row.name.casefold(), row.name))
        start = (query.page - 1) * PAGE_SIZE
        matches = [row for row in rows if term in row.name.casefold() and (
            query.kind in ("all", "folder") or
            not row.stat.is_dir and (is_image(row.name) if query.kind == "image" else not is_image(row.name)))]
        page = [entry(safe_join(query.path, row.name) if query.path else row.name, row.name, row.stat)
                for row in matches[start:start + PAGE_SIZE]]
        return DirectoryPage(root_name=root.root.rsplit("/", 1)[-1], path=query.path, page=query.page, page_size=PAGE_SIZE,
                             total=len(matches), folders=[row for row in page if row.is_dir],
                             files=[row for row in page if not row.is_dir])

    def info(self, login: LoginContext, path: str) -> FileInfo:
        root = self.root(login)
        item = entry(path, (path or root.root).rsplit("/", 1)[-1], root.info(path))
        result = FileInfo(**item.model_dump())
        if not item.is_dir:
            absolute = safe_join(root.root, path)
            formal = root.fs.paths.formal
            if absolute.startswith(formal + "/"):
                relative = absolute[len(formal) + 1:]
                physical = func.coalesce(CatalogFile.physical_rel_path, CatalogFile.rel_path)
                with self.auth.database.session() as session:
                    possible = session.scalar(select(CatalogFile.id).join(Project, Project.id == CatalogFile.project_id)
                        .where(Project.status.in_(("active", "voided")), CatalogFile.status.in_(("active", "voided")),
                               Project.current_path + "/" + physical == relative).limit(1))
                if possible is not None:
                    result.catalog_status = "unverified"
        return result

    @contextmanager
    def open(self, login: LoginContext, path: str) -> Iterator[tuple[FileEntry, BinaryIO]]:
        with self.root(login).open(path) as (metadata, stream):
            yield entry(path, path.rsplit("/", 1)[-1], metadata), stream

    def preview(self, login: LoginContext, path: str, size: int) -> bytes:
        if not self.preview_slots.acquire(timeout=8):
            raise AppError("preview_busy", "预览繁忙，请稍后重试", 503)
        try:
            with self.open(login, path) as (item, stream):
                if item.kind != "image":
                    raise AppError("preview_unsupported", "此格式请下载后查看", 415)
                if item.size > PREVIEW_MAX_BYTES:
                    raise AppError("preview_too_large", "原图过大，请下载后查看", 413)
                try:
                    return render_preview(stream, size)
                except ImageDecodeError as error:
                    raise AppError("preview_unavailable", str(error), 415) from None
        finally:
            self.preview_slots.release()

    def record(self, login: LoginContext, host: str, path: str | None, result: str) -> None:
        with self.auth.database.session() as session:
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "NAS 文件下载", "nas_file", None,
                                        None, {"path": path} if path else None, result, host)
