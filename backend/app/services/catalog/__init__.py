"""卡片柜的只读视图与下载：项目、商品、文件；文件读取一律用当前登录人本人的 NAS 会话。"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
import re
import tempfile
from typing import BinaryIO
from urllib.parse import quote

from PIL import Image, ImageDraw
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, CatalogFile, HistoryVersion, Industry, InspectionFinding, Link, Oplog, Product, ProductRelation,
                           ProductTag, Project, TagField, User)
from app.errors import AppError
from app.schemas.catalog import (DownloadItem, FileCheck, FileRow, FolderCount, HistoryRow, LinkTarget, LogRow, ProductBrief,
                                 ProductDetail, ProjectDetail, ProjectPage, ProjectRow, TagRow)
from app.services import archive
from app.services.auth.service import AuthService
from app.services.links import resolve
from app.services.nas import NasFs, NasNotFound, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED
from app.services.thumbs.images import render_variants
from app.services.upload.fingerprint import fingerprint

PAGE_SIZE = 20
CATEGORIES = [*REQUIRED, "套装", "单品", "other"]
SKIPPED = {*archive.ARCHIVE_DIRS.values(), archive.HISTORY_DIR, archive.PROJECT_ARCHIVE}
WON_FOLDERS = {"01已中标项目": True, "02未中标项目": False}
INACTIVE = {"voided": "已作废", "deleted": "已删除"}
CHUNK = 1024 * 1024


def _industry(path: str) -> str:
    parts = path.split("/")
    return re.sub(r"^\d{2}", "", parts[1]) if len(parts) >= 3 else ""


def content_disposition(name: str) -> str:
    """中文文件名用 RFC 5987 的 filename*，同时给不识别它的客户端一个 ASCII 回退名。"""
    fallback = re.sub(r'[^A-Za-z0-9._-]', "_", name) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """返回闭区间 (start, end)；没有或不认识的 Range 返回 None（发送整个文件）；无法满足时抛 416。"""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if match is None or (not match[1] and not match[2]):
        return None
    if not match[1]:
        length = int(match[2])
        if length == 0 or size == 0:
            raise AppError("range_not_satisfiable", "请求的范围超出文件大小", 416, {"size": size})
        return max(size - length, 0), size - 1
    start = int(match[1])
    end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start >= size or end < start:
        raise AppError("range_not_satisfiable", "请求的范围超出文件大小", 416, {"size": size})
    return start, end


@dataclass
class Download:
    stream: BinaryIO
    name: str
    size: int
    start: int
    end: int
    partial: bool
    etag: str
    modified: str

    def chunks(self) -> Iterator[bytes]:
        remaining = self.end - self.start + 1
        try:
            while remaining > 0:
                data = self.stream.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            self.stream.close()


def placeholder(size: int) -> bytes:
    side = min(size, 600)
    with Image.new("RGB", (side, side), (226, 232, 228)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle([side * 0.3, side * 0.36, side * 0.7, side * 0.64], outline=(150, 165, 158), width=max(2, side // 100))
        draw.line([side * 0.3, side * 0.64, side * 0.46, side * 0.48, side * 0.56, side * 0.57, side * 0.7, side * 0.42], fill=(150, 165, 158), width=max(2, side // 100))
        stream = BytesIO()
        image.save(stream, "JPEG", quality=80)
        return stream.getvalue()


class CatalogService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.paths = create_paths(auth.config)
        self.thumbs = auth.config.paths.data_dir / "thumbs" / "product"

    # ---- 项目 ----
    @staticmethod
    def _row(session: Session, project: Project) -> ProjectRow:
        return ProjectRow(id=project.id, project_code=project.project_code, project_name=project.project_name, company_name=project.company_name,
            industry_name=_industry(project.current_path), won=project.won, current_path=project.current_path, status=project.status,
            ingested_at=project.ingested_at,
            products=session.scalar(select(func.count()).select_from(Product).where(Product.project_id == project.id)) or 0,
            files=session.scalar(select(func.count()).select_from(CatalogFile).where(CatalogFile.project_id == project.id, CatalogFile.status == "active")) or 0)

    @staticmethod
    def _link(session: Session, kind: str, target_id: int) -> str | None:
        return session.scalar(select(Link.link_id).where(Link.kind == kind, Link.target_id == target_id))

    def projects(self, q: str, won: bool | None, industry: int | None, company: int | None, page: int) -> ProjectPage:
        with self.auth.lock, self.auth.database.session() as session:
            query = select(Project)
            text = q.strip()
            if text:
                like = f"%{text.replace('%', r'\%').replace('_', r'\_')}%"
                query = query.where(or_(*(column.ilike(like, escape="\\") for column in
                                          (Project.project_code, Project.project_name, Project.company_name, Project.folder_name))))
            if won is not None:
                query = query.where(Project.won.is_(won))
            if industry is not None:
                query = query.where(Project.industry_id == industry)
            if company is not None:
                query = query.where(Project.company_id == company)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = session.scalars(query.order_by(Project.ingested_at.desc(), Project.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
            return ProjectPage(items=[self._row(session, row) for row in rows], total=total, page=page, page_size=PAGE_SIZE)

    @staticmethod
    def _project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AppError("project_not_found", "项目不存在", 404)
        return project

    def _brief(self, session: Session, product: Product, related: list[int]) -> ProductBrief:
        return ProductBrief(id=product.id, product_code=product.product_code, display_name=product.display_name, kind=product.kind,
                            status=product.status, link_id=self._link(session, "product", product.id), related=related)

    def project(self, project_id: int) -> ProjectDetail:
        with self.auth.lock, self.auth.database.session() as session:
            project = self._project(session, project_id)
            products = list(session.scalars(select(Product).where(Product.project_id == project.id).order_by(Product.product_code)))
            ids = [row.id for row in products]
            relations = list(session.scalars(select(ProductRelation).where(ProductRelation.set_product_id.in_(ids)))) if ids else []
            counts = dict(session.execute(select(CatalogFile.category, func.count()).where(CatalogFile.project_id == project.id,
                                                                                         CatalogFile.status == "active").group_by(CatalogFile.category)).all())
            batch_ids = [str(value) for value in session.scalars(select(Batch.id).where(Batch.project_id == project.id))]
            logs = session.scalars(select(Oplog).where(or_((Oplog.object_type == "project") & (Oplog.object_id == str(project.id)),
                (Oplog.object_type == "batch") & Oplog.object_id.in_(batch_ids))).order_by(Oplog.id.desc()).limit(200))
            users = {row.id: (row.display_name or row.username) for row in session.scalars(select(User))}
            return ProjectDetail(project=self._row(session, project), folder_name=project.folder_name, code_source=project.code_source,
                link_id=self._link(session, "project", project.id),
                products_list=[self._brief(session, row, [item.item_product_id for item in relations if item.set_product_id == row.id] if row.kind == "set"
                               else [item.set_product_id for item in relations if item.item_product_id == row.id]) for row in products],
                folders=[FolderCount(name=name, count=counts.get(name, 0)) for name in CATEGORIES if name in REQUIRED or counts.get(name)],
                history=[HistoryRow(id=row.id, file_id=row.file_id, old_rel_path=row.old_rel_path, new_rel_path=row.new_rel_path, reason=row.reason,
                                    created_at=row.created_at) for row in session.scalars(select(HistoryVersion).where(HistoryVersion.project_id == project.id).order_by(HistoryVersion.id.desc()))],
                logs=[LogRow(ts=row.ts, user=users.get(row.user_id, "系统") if row.user_id else "系统", action=row.action, result=row.result) for row in logs])

    def files(self, project_id: int, category: str | None) -> list[FileRow]:
        with self.auth.lock, self.auth.database.session() as session:
            project = self._project(session, project_id)
            if category is not None and category not in CATEGORIES:
                raise AppError("invalid_category", "资料夹不存在", 422)
            query = select(CatalogFile).where(CatalogFile.project_id == project.id, CatalogFile.status == "active")
            if category is not None:
                query = query.where(CatalogFile.category == category)
            return [FileRow(id=row.id, name=row.rel_path.rsplit("/", 1)[-1], rel_path=row.rel_path, size=row.size, mtime=row.mtime, status=row.status,
                            category=row.category, link_id=self._link(session, "file", row.id)) for row in session.scalars(query.order_by(CatalogFile.rel_path))]

    def product(self, product_id: int) -> ProductDetail:
        with self.auth.lock, self.auth.database.session() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise AppError("product_not_found", "商品不存在", 404)
            project = session.get(Project, product.project_id)
            file = session.get(CatalogFile, product.file_id)
            fields = {row.key: (row.label, row.sort_order) for row in session.scalars(select(TagField))}
            tags = sorted(session.scalars(select(ProductTag).where(ProductTag.product_id == product.id)), key=lambda row: (fields.get(row.field_key, ("", 10 ** 6))[1], row.field_key))
            if product.kind == "set":
                related_ids = list(session.scalars(select(ProductRelation.item_product_id).where(ProductRelation.set_product_id == product.id)))
            else:
                related_ids = list(session.scalars(select(ProductRelation.set_product_id).where(ProductRelation.item_product_id == product.id)))
            related = sorted((session.get(Product, value) for value in related_ids), key=lambda row: row.product_code)
            return ProductDetail(id=product.id, product_code=product.product_code, display_name=product.display_name, kind=product.kind,
                status=product.status, free_text=product.free_text, file_id=file.id, image_rel_path=file.rel_path,
                tags=[TagRow(key=row.field_key, label=fields.get(row.field_key, (row.field_key, 0))[0], value=row.value, terms=list(row.terms), source=row.source) for row in tags],
                related=[self._brief(session, row, []) for row in related], project=self._row(session, project),
                product_link=self._link(session, "product", product.id), project_link=self._link(session, "project", project.id), ingested_at=product.ingested_at)

    # ---- 四层查找 ----
    def _absolute(self, project: Project, rel_path: str) -> str:
        return self.paths.join(Region.FORMAL, project.current_path, rel_path)

    def _find_project_folder(self, fs: NasFs, project: Project) -> str | None:
        """正式目录固定为「中标状态/行业/企业/项目」四层，按立项编号前缀认出被整体移动的项目文件夹。"""
        root = self.paths.root(Region.FORMAL)
        prefix = project.project_code + "_"
        for won in fs.list(root):
            for industry in self._list_dirs(fs, f"{root}/{won}"):
                for company in self._list_dirs(fs, f"{root}/{won}/{industry}"):
                    for folder in self._list_dirs(fs, f"{root}/{won}/{industry}/{company}"):
                        if folder.startswith(prefix):
                            return f"{won}/{industry}/{company}/{folder}"
        return None

    @staticmethod
    def _list_dirs(fs: NasFs, path: str) -> list[str]:
        try:
            return [name for name in fs.list(path) if fs.stat(f"{path}/{name}").is_dir]
        except NasNotFound:
            return []

    def _walk(self, fs: NasFs, root: str, prefix: str = "") -> Iterator[tuple[str, int, int]]:
        for name in fs.list(f"{root}/{prefix}".rstrip("/")):
            if not prefix and name in SKIPPED:
                continue
            rel_path = f"{prefix}{name}"
            stat = fs.stat(f"{root}/{rel_path}")
            if stat.is_dir:
                yield from self._walk(fs, root, rel_path + "/")
            else:
                yield rel_path, stat.size, int(stat.mtime * 1000)

    def locate(self, session: Session, fs: NasFs, file: CatalogFile, actor: User, host: str) -> str:
        """返回文件当前的 NAS 路径；路径变了就更新卡片柜，四层都找不到则登记待巡检并报「文件已不存在」。"""
        project = session.get(Project, file.project_id)
        # 第一层：卡片柜记录的路径。
        path = self._absolute(project, file.rel_path)
        if fs.exists(path):
            return path
        log = OplogService(session)
        # 第二层：按立项编号重新定位项目文件夹，再拼相对路径。
        if not fs.exists(self.paths.join(Region.FORMAL, project.current_path)):
            found = self._find_project_folder(fs, project)
            if found is not None and found != project.current_path:
                before = {"current_path": project.current_path, "won": project.won}
                project.current_path, project.folder_name = found, found.rsplit("/", 1)[-1]
                project.won = WON_FOLDERS.get(found.split("/")[0], project.won)
                log.write(actor.id, "system", "更新项目路径", "project", str(project.id), before,
                          {"current_path": project.current_path, "won": project.won, "by": "按立项编号定位"}, "成功", host)
                path = self._absolute(project, file.rel_path)
                if fs.exists(path):
                    return path
        root = self.paths.join(Region.FORMAL, project.current_path)
        if fs.exists(root):
            taken = set(session.scalars(select(CatalogFile.rel_path).where(CatalogFile.project_id == project.id, CatalogFile.id != file.id)))
            entries = [entry for entry in self._walk(fs, root) if entry[0] not in taken]
            name = file.rel_path.rsplit("/", 1)[-1]
            # 第三层：项目内按文件名、大小、修改时间匹配，唯一命中才采信。
            matches = [entry for entry in entries if entry[0].rsplit("/", 1)[-1] == name and entry[1] == file.size and entry[2] == file.mtime]
            how = "文件名、大小与修改时间"
            if len(matches) != 1:
                # 第四层：大小相同的候选逐个计算指纹确认。
                threshold = self.auth.config.upload.full_fingerprint_max_mb * 1024 * 1024
                matches = [entry for entry in entries if entry[1] == file.size
                           and fingerprint(fs, f"{root}/{entry[0]}", entry[1], threshold)[0] == file.fingerprint][:1]
                how = "指纹"
            if matches:
                before = {"rel_path": file.rel_path}
                file.rel_path, file.mtime = matches[0][0], matches[0][2]
                file.category = file.rel_path.split("/")[0] if "/" in file.rel_path and file.rel_path.split("/")[0] in CATEGORIES else "other"
                log.write(actor.id, "system", "更新文件路径", "file", str(file.id), before, {"rel_path": file.rel_path, "by": how}, "成功", host)
                return self._absolute(project, file.rel_path)
        if session.scalar(select(InspectionFinding.id).where(InspectionFinding.file_id == file.id, InspectionFinding.kind == "missing",
                                                              InspectionFinding.run_id.is_(None))) is None:
            session.add(InspectionFinding(kind="missing", path=f"{project.current_path}/{file.rel_path}", project_id=project.id, file_id=file.id,
                                          auto_fixed=False, detail="下载时四层查找均未找到"))
        log.write(actor.id, "system", "文件丢失待巡检", "file", str(file.id), None, {"rel_path": file.rel_path}, "失败", host)
        raise _Missing()

    # ---- 下载 ----
    def _file(self, session: Session, file_id: int) -> CatalogFile:
        file = session.get(CatalogFile, file_id)
        if file is None:
            raise AppError("file_not_found", "文件不存在", 404)
        if file.status == "deleted":
            raise AppError("file_deleted", "文件已删除", 410)
        return file

    def _locate(self, file_id: int, actor: User, host: str) -> tuple[NasFs, str, CatalogFile]:
        fs = self.auth.pool.get(actor.id)
        missing = False
        with self.auth.lock, self.auth.database.session() as session:
            file = self._file(session, file_id)
            try:
                path = self.locate(session, fs, file, actor, host)
            except _Missing:
                # 让「待巡检」记录随事务提交，再向调用方报错。
                missing, path = True, ""
            session.flush()
            session.expunge(file)
        if missing:
            raise AppError("file_missing", "文件已不存在", 404)
        return fs, path, file

    def check(self, file_id: int, actor: User, host: str) -> FileCheck:
        """下载前的探测：权限、是否存在（含四层查找）与实际大小；不记下载日志。"""
        fs, path, file = self._locate(file_id, actor, host)
        with fs.open_read(path) as stream:
            stream.read(1)
        return FileCheck(id=file.id, name=file.rel_path.rsplit("/", 1)[-1], size=fs.stat(path).size)

    def download(self, file_id: int, range_header: str | None, if_range: str | None, actor: User, host: str) -> Download:
        fs, path, file = self._locate(file_id, actor, host)
        stat = fs.stat(path)
        etag = f'"{file.fingerprint}"'
        modified = format_datetime(datetime.fromtimestamp(int(stat.mtime), timezone.utc), usegmt=True)
        # 续传时带 If-Range：文件没变才按范围发送，否则重发整个文件。
        span = parse_range(range_header, stat.size) if not if_range or if_range in (etag, modified) else None
        start, end = span if span else (0, stat.size - 1)
        stream = fs.open_read(path, offset=start)
        try:
            with self.auth.lock, self.auth.database.session() as session:
                user = session.get(User, actor.id)
                OplogService(session).write(actor.id, ",".join(user.roles) if user else "", "下载文件", "file", str(file.id), None,
                    {"rel_path": file.rel_path, "project_id": file.project_id, "range": [start, end] if span else None}, "成功", host)
        except Exception:
            stream.close()
            raise
        return Download(stream=stream, name=file.rel_path.rsplit("/", 1)[-1], size=stat.size, start=start, end=end, partial=span is not None,
                        etag=etag, modified=modified)

    def download_many(self, file_ids: list[int]) -> list[DownloadItem]:
        with self.auth.lock, self.auth.database.session() as session:
            return [DownloadItem(file_id=file.id, name=file.rel_path.rsplit("/", 1)[-1], size=file.size, url=f"/api/files/{file.id}/download")
                    for file in (self._file(session, value) for value in dict.fromkeys(file_ids))]

    # ---- 商品缩略图 ----
    def thumbnail(self, product_id: int, size: int, actor: User) -> tuple[bytes, bool]:
        """返回 (JPEG, 是否占位图)。缓存缺失时用当前用户会话从 NAS 重新生成。"""
        if size not in (300, 1600, 1024):
            raise AppError("invalid_thumbnail_size", "图片尺寸不支持", 422)
        with self.auth.lock, self.auth.database.session() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise AppError("product_not_found", "商品不存在", 404)
            file = session.get(CatalogFile, product.file_id)
            path = self._absolute(session.get(Project, product.project_id), file.rel_path)
        target = self.thumbs / str(product_id) / f"{size}.jpg"
        if not target.is_file():
            try:
                fs = self.auth.pool.get(actor.id)
                self.thumbs.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="thumb-", dir=self.thumbs) as stage:
                    with fs.open_read(path) as stream:
                        render_variants(stream, Path(stage))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for value in (300, 1600, 1024):
                        (Path(stage) / f"{value}.jpg").replace(target.parent / f"{value}.jpg")
            except Exception:
                # 无权限、文件不在、图片损坏或会话失效都不让页面开天窗，统一给占位图。
                return placeholder(size), True
        return target.read_bytes(), False

    # ---- 永久链接 ----
    def link(self, link_id: str) -> LinkTarget:
        with self.auth.lock, self.auth.database.session() as session:
            found = resolve(session, link_id)
            if found is None:
                raise AppError("link_not_found", "链接不存在", 404)
            kind, target_id = found
            if kind == "product":
                target, url, name = session.get(Product, target_id), f"/products/{target_id}", "商品"
            elif kind == "file":
                target, url, name = session.get(CatalogFile, target_id), f"/api/files/{target_id}/download", "文件"
            else:
                target, url, name = session.get(Project, target_id), f"/projects/{target_id}", "项目"
            status = target.status if target is not None else "deleted"
            if status in INACTIVE:
                return LinkTarget(link_id=link_id, kind=kind, target_id=target_id, status=status, url=None, message=f"该{name}{INACTIVE[status]}")
            return LinkTarget(link_id=link_id, kind=kind, target_id=target_id, status=status, url=url, message=None)


class _Missing(Exception):
    pass
