"""按稳定身份解析链接，并使用访问者本人的 NAS 读权限。"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import CatalogFile, HistoryVersion, Link, Product, Project
from app.errors import AppError
from app.schemas.ingest import ResolvedLink
from app.services.auth.service import AuthService, LoginContext
from app.services.nas import Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.upload.service import UploadService


class LinkService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    def resolve(self, link_id: str, login: LoginContext, host: str) -> ResolvedLink:
        with self.auth.lock, self.auth.database.session() as session:
            UploadService._check_login(session, login)
            link = session.scalar(select(Link).where(Link.link_id == link_id))
            missing = AppError("link_not_found", "找不到可访问的永久链接", 404)
            if link is None or link.kind not in ("project", "product", "file"):
                raise missing
            product = session.get(Product, link.target_id) if link.kind == "product" else None
            file = session.get(CatalogFile, product.file_id if product else link.target_id) if link.kind in ("product", "file") else None
            if link.kind == "product" and (not product or not file) or link.kind == "file" and not file:
                raise missing
            project = session.get(Project, file.project_id if file else link.target_id)
            if project is None or project.status in ("ingesting", "ingest_error"):
                raise missing
            if file and product and product.project_id != file.project_id:
                raise missing
            state = product.status if product else file.status if file else project.status
            if project.status in ("voided", "deleted"):
                state = project.status
            fs = self.auth.pool.get(login.session_id)
            paths = create_paths(self.auth.config)
            root = paths.join(Region.FORMAL, project.current_path)
            if state != "deleted":
                if file is not None:
                    old = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file.id).order_by(HistoryVersion.id.desc()))
                    path = paths.join(Region.FORMAL, project.current_path, old.new_rel_path if old else file.rel_path)
                    fs.stat(path)
                else:
                    fs.list(root)
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "解析永久链接", link.kind,
                                        str(link.target_id), None, {"project_id": project.id}, "成功", host)
            return ResolvedLink(kind=link.kind, link_id=link_id, target_id=link.target_id, project_id=project.id,
                url=f"{self.auth.config.server.public_base_url.rstrip('/')}/l/{link_id}", status=state,
                image_file_id=product.file_id if product else None)
