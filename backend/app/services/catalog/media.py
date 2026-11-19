"""单文件流、Range 与预览授权。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import re
import secrets
from typing import Iterator
from urllib.parse import quote, urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AccessLog, IngestPlan, Product, Project, User, utcnow
from app.errors import AppError
from app.schemas.catalog import DownloadItem, DownloadProgress, FileCheck
from app.services.auth.service import AuthService, LoginContext
from app.services.nas import NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, Region
from app.services.oplog import OplogService
from app.services.thumbs.images import ImageDecodeError, VARIANT_SIZES, render_variants
from app.services.upload.service import UploadService
from .locator import AssetLocator, CHUNK_SIZE, ReadAsset, reference

NO_CACHE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def is_image(name: str) -> bool:
    return name.lower().endswith(IMAGE_SUFFIXES)


class RangeError(AppError):
    def __init__(self, size: int) -> None:
        super().__init__("invalid_range", "下载范围无效", 416)
        self.size = size


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", value.strip(), re.IGNORECASE)
    if not match or size == 0 or not (match[1] or match[2]):
        raise RangeError(size)
    if not match[1]:
        count = int(match[2])
        if count == 0:
            raise RangeError(size)
        return max(0, size - count), size - 1
    start = int(match[1])
    end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start >= size or end < start:
        raise RangeError(size)
    return start, end


def stream_headers(asset: ReadAsset, span: tuple[int, int] | None, *, inline: bool) -> dict[str, str]:
    name = asset.name
    fallback = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "file"
    length = asset.size if span is None else span[1] - span[0] + 1
    disposition = "inline" if inline else "attachment"
    headers = {**NO_CACHE, "Accept-Ranges": "bytes", "Content-Length": str(length),
               "ETag": f'"{asset.digest}"',
               "Last-Modified": format_datetime(datetime.fromtimestamp(int(asset.mtime), timezone.utc), usegmt=True),
               "Content-Disposition": f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"}
    if span:
        headers["Content-Range"] = f"bytes {span[0]}-{span[1]}/{asset.size}"
    if asset.warning:
        headers["X-Asset-Warning"] = "content_changed"
    if asset.status in ("voided", "replaced"):
        headers["X-Asset-State"] = asset.status
    return headers


def transfer_chunks(asset: ReadAsset, length: int) -> Iterator[bytes]:
    remaining = length
    while remaining:
        chunk = asset.stream.read(min(remaining, CHUNK_SIZE))
        if not chunk:
            raise NasUnavailable("NAS 文件流提前结束")
        remaining -= len(chunk)
        yield chunk


class MediaService:
    def __init__(self, auth: AuthService, locator: AssetLocator) -> None:
        self.auth, self.locator = auth, locator
        self.cache = auth.config.paths.data_dir / "thumbs"

    def _check_login(self, session: Session, login: LoginContext) -> None:
        UploadService._check_login(session, login)
        actor = session.get(User, login.user.id)
        if actor is None or not actor.enabled:
            raise AppError("login_required", "账号已停用，请重新登录", 401)

    @contextmanager
    def open(self, file_id: int, login: LoginContext, host: str) -> Iterator[ReadAsset]:
        with self.auth.lock, self.auth.database.session() as session:
            self._check_login(session, login)
            ref = reference(session, file_id)
            fs = self.auth.pool.pin(login.session_id)
        asset = None
        try:
            asset = self.locator.open(fs, ref, login, host)
            with self.auth.lock, self.auth.database.session() as session:
                self._check_login(session, login)
                current = reference(session, file_id)
                if current.file.fingerprint != ref.file.fingerprint or current.project.status != ref.project.status:
                    raise AppError("asset_changed", "文件状态已变化，请重试", 409)
            yield asset
        finally:
            try:
                if asset is not None:
                    asset.stream.close()
            finally:
                self.auth.pool.unpin(login.session_id)

    def authorize_main_image(self, file_id: int, login: LoginContext) -> None:
        with self.auth.database.session() as session:
            self._check_login(session, login)
            ref = reference(session, file_id)
            if ref.project.status != "active" or ref.file.status != "active":
                raise AppError("file_not_found", "文件不存在", 404)
        fs = self.auth.pool.get(login.session_id)
        with fs.open_read(self.locator.paths.join(Region.FORMAL, ref.project.current_path, ref.relative)) as stream:
            stream.read(1)

    def record(self, login: LoginContext, kind: str, file_id: int, host: str, outcome: str,
               transfer: str | None = None, *, resume: bool = False, track: bool = True) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            query = select(AccessLog).where(AccessLog.session_id == login.session_id, AccessLog.kind == kind,
                AccessLog.target_id == file_id)
            logical_id = f"{transfer}.{outcome}" if transfer else secrets.token_urlsafe(16)
            if transfer:
                previous = session.scalar(query.where(AccessLog.logical_id == logical_id))
            elif resume or kind == "preview":
                interval = timedelta(hours=self.auth.config.server.session_hours) if resume else timedelta(minutes=1)
                previous = session.scalar(query.where(AccessLog.ts >= utcnow() - interval, AccessLog.outcome == outcome)
                    .order_by(AccessLog.id.desc()).limit(1))
            else:
                previous = None
            delivery = "sending" if outcome in ("allowed", "warning") else "failed"
            tracked = transfer is not None and kind == "download" and track
            if previous is not None:
                if tracked:
                    previous.delivery, previous.delivery_at = delivery, utcnow()
                return
            session.add(AccessLog(user_id=login.user.id, session_id=login.session_id, kind=kind,
                                  target_id=file_id, logical_id=logical_id, outcome=outcome, client_host=host,
                                  delivery=delivery if tracked else None, delivery_at=utcnow() if tracked else None))
            action = "预览文件" if kind == "preview" else "下载文件"
            OplogService(session).write(login.user.id, ",".join(login.user.roles), action, "file", str(file_id), None,
                                        {"outcome": outcome}, "成功" if outcome == "allowed" else "异常", host)

    @staticmethod
    def _transfer_query(login: LoginContext, file_id: int, transfer: str):
        prefix = transfer + "."
        return select(AccessLog).where(AccessLog.session_id == login.session_id, AccessLog.kind == "download",
            AccessLog.target_id == file_id, func.substr(AccessLog.logical_id, 1, len(prefix)) == prefix)

    def delivery(self, login: LoginContext, file_id: int, transfer: str) -> DownloadProgress:
        with self.auth.database.session() as session:
            self._check_login(session, login)
            latest = session.scalar(self._transfer_query(login, file_id, transfer).order_by(AccessLog.delivery_at.desc(), AccessLog.id.desc()).limit(1))
            if latest is None:
                return DownloadProgress(status="pending")
            explanations = {"nas_permission_denied": "NAS 无权限", "file_missing": "文件已不存在",
                "content_changed": "文件内容已变化，请打开文件页核对警告",
                "nas_session_expired": "NAS 连接已超时，请重新登录", "file_deleted": "文件已物理删除"}
            message = explanations.get(latest.outcome, "下载失败，请重试") if latest.delivery == "failed" else None
            return DownloadProgress(status=latest.delivery or "pending", message=message)

    def delivered(self, login: LoginContext, file_id: int, transfer: str, complete: bool) -> None:
        with self.auth.database.session() as session:
            row = session.scalar(self._transfer_query(login, file_id, transfer).where(
                AccessLog.outcome.in_(("allowed", "warning"))).order_by(AccessLog.id.desc()).limit(1))
            if row is not None:
                row.delivery, row.delivery_at = ("sent" if complete else "interrupted"), utcnow()

    def interrupted(self, login: LoginContext, file_id: int, host: str) -> None:
        with self.auth.database.session() as session:
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "文件传输中断", "file",
                                        str(file_id), None, None, "失败", host)

    def check(self, file_id: int, login: LoginContext, host: str) -> FileCheck:
        with self.open(file_id, login, host) as asset:
            params = {"transfer": secrets.token_urlsafe(16)}
            if asset.warning:
                params["allow_changed"] = "true"
            return FileCheck(id=file_id, name=asset.name, size=asset.size, status=asset.status,
                is_image=is_image(asset.name), warning=asset.warning,
                url=f"/api/files/{file_id}/download?{urlencode(params)}")

    def many(self, file_ids: list[int], login: LoginContext, host: str) -> list[DownloadItem]:
        items = []
        for file_id in dict.fromkeys(file_ids):
            try:
                info = self.check(file_id, login, host)
                if info.warning:
                    raise AppError("content_changed", info.warning, 409)
                items.append(DownloadItem(file_id=file_id, name=info.name, size=info.size, url=info.url))
            except (AppError, NasNotFound, NasPermissionDenied, NasUnavailable) as error:
                code, message = public_error(error)
                self.record(login, "download", file_id, host, code)
                items.append(DownloadItem(file_id=file_id, code=code, error=message))
        return items

    def _official_thumb(self, asset: ReadAsset, product_id: int | None, size: int) -> bytes | None:
        if asset.warning or asset.reference.file.first_batch_id is None:
            return None
        with self.auth.database.session() as session:
            plan = session.get(IngestPlan, asset.reference.file.first_batch_id)
            if not plan or not any(row["id"] == asset.reference.file.id and row["fingerprint"] == asset.digest
                                   for row in plan.manifest["files"]):
                return None
            origin = next((row["id"] for row in plan.manifest["products"] if row["file_id"] == asset.reference.file.id
                           and (product_id is None or row["id"] == product_id)), None)
        if origin is None:
            return None
        target = self.cache / "product" / str(origin) / f"{size}.jpg"
        return target.read_bytes() if target.is_file() else None

    def preview(self, file_id: int, size: int, login: LoginContext, host: str, *,
                product_id: int | None = None, allow_changed: bool = False, transfer: str | None = None) -> tuple[bytes, dict[str, str]]:
        if size not in VARIANT_SIZES:
            raise AppError("invalid_thumbnail_size", "图片尺寸不支持", 422)
        try:
            with self.open(file_id, login, host) as asset:
                if not is_image(asset.name):
                    raise AppError("preview_unsupported", "该格式请下载后查看", 415)
                if asset.warning and not allow_changed:
                    raise AppError("content_changed", asset.warning, 409)
                picture = self._official_thumb(asset, product_id, size)
                if picture is None:
                    directory = self.cache / "catalog" / str(file_id) / asset.digest
                    target = directory / f"{size}.jpg"
                    if not target.is_file():
                        render_variants(asset.stream, directory)
                    picture = target.read_bytes()
                warning = bool(asset.warning)
        except ImageDecodeError:
            self.record(login, "preview", file_id, host, "decode_error", transfer)
            raise AppError("image_invalid", "图片无法解码，请下载后查看", 422) from None
        except (AppError, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable) as error:
            self.record(login, "preview", file_id, host, public_error(error)[0], transfer)
            raise
        self.record(login, "preview", file_id, host, "warning" if warning else "allowed", transfer)
        return picture, {**NO_CACHE, **({"X-Asset-Warning": "content_changed"} if warning else {})}

    def main_image_id(self, product_id: int) -> int:
        with self.auth.database.session() as session:
            product = session.get(Product, product_id)
            if product is None or product.status != "active":
                raise AppError("product_not_found", "商品不存在或已作废", 404)
            project = session.get(Project, product.project_id)
            if project is None or project.status != "active":
                raise AppError("product_not_found", "商品不存在或已作废", 404)
            ref = reference(session, product.file_id)
            if ref.project.status != "active" or ref.file.project_id != product.project_id or ref.file.status != "active":
                raise AppError("product_not_found", "商品不存在或已作废", 404)
            return product.file_id


def public_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, AppError):
        return error.code, error.message
    if isinstance(error, NasPermissionDenied):
        return "nas_permission_denied", "NAS 无权限"
    if isinstance(error, NasSessionExpired):
        return "nas_session_expired", "NAS 连接已超时，请重新登录"
    if isinstance(error, NasNotFound):
        return "file_missing", "文件已不存在"
    return "nas_unavailable", "NAS 暂时不可用"
