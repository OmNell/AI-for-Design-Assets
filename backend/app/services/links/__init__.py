"""复用 WP09 的永久链接标识，按访问者权限解析稳定对象。"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import CatalogFile, HistoryVersion, Link, Product, Project
from app.errors import AppError
from app.schemas.ingest import ResolvedLink
from app.services.auth.service import AuthService, LoginContext
from app.services.catalog import CatalogService
from app.services.catalog.locator import VISIBLE_PROJECTS
from app.services.oplog import OplogService


class LinkService:
    def __init__(self, auth: AuthService, catalog: CatalogService) -> None:
        self.auth, self.catalog = auth, catalog

    def resolve(self, link_id: str, login: LoginContext, host: str) -> ResolvedLink:
        missing = AppError("link_not_found", "找不到可访问的永久链接", 404)
        with self.auth.database.session() as session:
            self.catalog.media._check_login(session, login)
            link = session.scalar(select(Link).where(Link.link_id == link_id))
            if link is None or link.kind not in ("project", "product", "file"):
                raise missing
            product = session.get(Product, link.target_id) if link.kind == "product" else None
            file = session.get(CatalogFile, product.file_id if product else link.target_id) if link.kind in ("product", "file") else None
            if (link.kind == "product" and (not product or not file)) or (link.kind == "file" and not file):
                raise missing
            project = session.get(Project, file.project_id if file else link.target_id)
            if project is None or project.status not in VISIBLE_PROJECTS:
                raise missing
            if file and product and product.project_id != file.project_id:
                raise missing
            state = product.status if product else file.status if file else project.status
            if project.status in ("voided", "deleted"):
                state = project.status
            if file:
                latest = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file.id)
                                        .order_by(HistoryVersion.id.desc()).limit(1))
                if file.status == "deleted" or (latest and latest.expected_missing):
                    state = "deleted"
        if state != "deleted":
            if file is not None:
                with self.catalog.media.open(file.id, login, host):
                    pass
            else:
                self.catalog.authorize_project(project, login, host, relocate=True)
        with self.auth.database.session() as session:
            self.catalog.media._check_login(session, login)
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "解析永久链接", link.kind,
                                        str(link.target_id), None, {"project_id": project.id}, "成功", host)
        return ResolvedLink(kind=link.kind, link_id=link_id, target_id=link.target_id, project_id=project.id,
            url=self.catalog.url(link_id), status=state, image_file_id=product.file_id if product else None,
            deleted_at=next((row.deleted_at for row in (product, file, project) if row and row.deleted_at), None),
            delete_reason=next((row.delete_reason for row in (product, file, project) if row and row.delete_reason), None))
