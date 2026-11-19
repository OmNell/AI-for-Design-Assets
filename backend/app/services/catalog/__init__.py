"""正式卡片柜的只读项目、商品和资料列表。"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, CatalogFile, CompanyIndustryMap, HistoryVersion, Industry, InspectionFinding, Link,
                           Oplog, Product, ProductRelation, ProductTag, Project, User)
from app.errors import AppError
from app.schemas.catalog import (AssetFinding, FileRow, FolderCount, HistoryRow, LogRow, ProductBrief, ProductDetail,
                                 ProjectDetail, ProjectFilters, ProjectPage, ProjectRow, TagRow)
from app.services.ai.field_catalog import FIXED_FIELDS, fixed_fields
from app.services.auth.service import AuthService, LoginContext
from app.services.nas import NasNotFound, NasPermissionDenied, Region
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED
from .locator import AssetLocator
from .media import MediaService, is_image


class CatalogService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.locator = AssetLocator(auth)
        self.media = MediaService(auth, self.locator)

    def url(self, link_id: str | None) -> str | None:
        return f"{self.auth.config.server.public_base_url.rstrip('/')}/l/{link_id}" if link_id else None

    @staticmethod
    def link_id(session: Session, kind: str, target_id: int) -> str | None:
        return session.scalar(select(Link.link_id).where(Link.kind == kind, Link.target_id == target_id))

    def authorize_project(self, project: Project, login: LoginContext, host: str, *, relocate: bool = False) -> None:
        fs = self.auth.pool.get(login.session_id)
        paths = self.locator.paths
        try:
            fs.list(paths.join(Region.FORMAL, project.current_path))
        except NasNotFound:
            if not relocate:
                raise
            with self.auth.database.session() as session:
                file_id = session.scalar(select(CatalogFile.id).where(CatalogFile.project_id == project.id,
                    CatalogFile.status.in_(("active", "voided", "replaced"))).order_by(CatalogFile.id).limit(1))
            if file_id is None:
                raise AppError("project_not_found", "项目位置不存在", 404) from None
            with self.media.open(file_id, login, host):
                pass
            with self.auth.database.session() as session:
                current = session.get(Project, project.id)
                fs.list(paths.join(Region.FORMAL, current.current_path))

    @staticmethod
    def load_project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None or project.status != "active":
            raise AppError("project_not_found", "项目不存在或已移出正式列表", 404)
        return project

    @staticmethod
    def _industry(session: Session, project: Project) -> str:
        archived = (project.archive_review or {}).get("industry")
        row = session.get(Industry, project.industry_id) if project.industry_id is not None else None
        return str(archived or (row.name if row else ""))

    def _row(self, session: Session, project: Project) -> ProjectRow:
        return ProjectRow(id=project.id, project_code=project.project_code, project_name=project.project_name,
            company_name=project.company_name, industry_name=self._industry(session, project), won=project.won,
            status=project.status, ingested_at=project.ingested_at, link_url=self.url(self.link_id(session, "project", project.id)),
            products=session.scalar(select(func.count()).select_from(Product).where(
                Product.project_id == project.id, Product.status == "active")),
            files=session.scalar(select(func.count()).select_from(CatalogFile).where(
                CatalogFile.project_id == project.id, CatalogFile.status == "active")))

    def selection(self, session: Session, filters: ProjectFilters) -> list[Project]:
        sort = {"newest": Project.ingested_at.desc(), "oldest": Project.ingested_at.asc(),
                "name": Project.project_name.asc(), "code": Project.project_code.asc()}
        query = select(Project).where(Project.status == "active")
        if filters.won is not None:
            query = query.where(Project.won == filters.won)
        companies = list(session.scalars(select(CompanyIndustryMap)))
        matching = []
        term = filters.q.strip().casefold()
        company_term = filters.company.strip().casefold()
        industry_term = filters.industry.strip().casefold()
        for project in session.scalars(query.order_by(sort[filters.sort], Project.id)):
            names = {project.company_name.casefold()}
            for row in companies:
                company_names = {item.casefold() for item in [row.company_name, *row.aliases]}
                if row.id == project.company_id or names.intersection(company_names):
                    names.update(company_names)
                    break
            if term and not any(term in value for value in (project.project_code.casefold(), project.project_name.casefold(), *names)):
                continue
            if company_term and str(project.company_id) != company_term and not any(company_term in value for value in names):
                continue
            if industry_term and str(project.industry_id) != industry_term and industry_term not in self._industry(session, project).casefold():
                continue
            matching.append(project)
        return matching

    def list(self, filters: ProjectFilters, login: LoginContext, host: str) -> ProjectPage:
        with self.auth.database.session() as session:
            self.media._check_login(session, login)
            matching = self.selection(session, filters)
        visible = []
        for project in matching:
            try:
                self.authorize_project(project, login, host)
            except (NasNotFound, NasPermissionDenied):
                continue
            visible.append(project.id)
        offset = (filters.page - 1) * filters.page_size
        with self.auth.database.session() as session:
            self.media._check_login(session, login)
            rows = [self._row(session, row) for pid in visible[offset:offset + filters.page_size]
                    if (row := session.get(Project, pid)) and row.status == "active"]
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "查找项目", "project", None, None,
                                        {"filters": filters.model_dump(), "matches": len(visible)}, "成功", host)
            return ProjectPage(items=rows, total=len(visible), page=filters.page, page_size=filters.page_size)

    def _brief(self, session: Session, product: Product, related: list[int]) -> ProductBrief:
        link = self.link_id(session, "product", product.id)
        tags = list(session.scalars(select(ProductTag.value).where(ProductTag.product_id == product.id,
            ProductTag.confirmation_status == "confirmed_value", ProductTag.field_key.in_(("category", "color", "material", "style")),
            ProductTag.value != "").order_by(ProductTag.id)))
        return ProductBrief(id=product.id, product_code=product.product_code, display_name=product.display_name,
                            kind=product.kind, status=product.status, link_id=link, link_url=self.url(link),
                            related=related, tags=tags)

    def _project_read(self, project_id: int, login: LoginContext, host: str) -> None:
        with self.auth.database.session() as session:
            self.media._check_login(session, login)
            project = self.load_project(session, project_id)
        self.authorize_project(project, login, host, relocate=True)

    def project(self, project_id: int, login: LoginContext, host: str) -> ProjectDetail:
        self._project_read(project_id, login, host)
        with self.auth.database.session() as session:
            project = self.load_project(session, project_id)
            products = list(session.scalars(select(Product).where(Product.project_id == project.id,
                Product.status == "active").order_by(Product.product_code)))
            ids = {row.id for row in products}
            relations: dict[int, list[int]] = {pid: [] for pid in ids}
            for row in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id.in_(ids),
                    ProductRelation.item_product_id.in_(ids), ProductRelation.status == "active")):
                relations[row.set_product_id].append(row.item_product_id)
                relations[row.item_product_id].append(row.set_product_id)
            counts = dict(session.execute(select(CatalogFile.category, func.count()).where(
                CatalogFile.project_id == project.id, CatalogFile.status == "active").group_by(CatalogFile.category)).all())
            batch_ids = [str(value) for value in session.scalars(select(Batch.id).where(Batch.project_id == project.id))]
            logs = session.scalars(select(Oplog).where(or_((Oplog.object_type == "project") & (Oplog.object_id == str(project.id)),
                (Oplog.object_type == "batch") & Oplog.object_id.in_(batch_ids))).order_by(Oplog.id.desc()).limit(100))
            history = session.scalars(select(HistoryVersion).where(HistoryVersion.project_id == project.id).order_by(HistoryVersion.id.desc()))
            users = {row.id: (row.display_name or row.username) for row in session.scalars(select(User))}
            return ProjectDetail(project=self._row(session, project), folder_name=project.folder_name, code_source=project.code_source,
                link_id=self.link_id(session, "project", project.id),
                products_list=[self._brief(session, row, relations[row.id]) for row in products],
                folders=[FolderCount(name=name, count=counts.get(name, 0)) for name in REQUIRED],
                history=[HistoryRow(id=row.id, file_id=row.file_id, old_rel_path=row.old_rel_path, new_rel_path=row.new_rel_path,
                                    reason=row.reason, created_at=row.created_at) for row in history],
                logs=[LogRow(ts=row.ts, user=users.get(row.user_id, "系统"), action=row.action, result=row.result) for row in logs])

    def files(self, project_id: int, category: str | None, login: LoginContext, host: str) -> list[FileRow]:
        if category is not None and category not in [*REQUIRED, "套装", "单品", "other"]:
            raise AppError("invalid_category", "资料夹不存在", 422)
        self._project_read(project_id, login, host)
        with self.auth.database.session() as session:
            self.load_project(session, project_id)
            query = select(CatalogFile).where(CatalogFile.project_id == project_id, CatalogFile.status == "active")
            if category is not None:
                query = query.where(CatalogFile.category == category)
            result = []
            for row in session.scalars(query.order_by(CatalogFile.rel_path)):
                name = row.rel_path.rsplit("/", 1)[-1]
                link = self.link_id(session, "file", row.id)
                result.append(FileRow(id=row.id, name=name, rel_path=row.rel_path, size=row.size, mtime=row.mtime,
                    status=row.status, category=row.category, link_id=link, link_url=self.url(link), is_image=is_image(name)))
            return result

    def product(self, product_id: int, login: LoginContext, host: str) -> ProductDetail:
        file_id = self.media.main_image_id(product_id)
        with self.auth.database.session() as session:
            product = session.get(Product, product_id)
        self._project_read(product.project_id, login, host)
        with self.auth.database.session() as session:
            project = self.load_project(session, product.project_id)
            product = session.get(Product, product_id)
            if product is None or product.status != "active" or product.file_id != file_id:
                raise AppError("product_not_found", "商品不存在或已作废", 404)
            related = ProductRelation.item_product_id if product.kind == "set" else ProductRelation.set_product_id
            own = ProductRelation.set_product_id if product.kind == "set" else ProductRelation.item_product_id
            related_rows = list(session.scalars(select(Product).where(Product.project_id == project.id,
                Product.status == "active", Product.id.in_(select(related).where(own == product.id,
                    ProductRelation.status == "active"))).order_by(Product.product_code)))
            tags = {row.field_key: row for row in session.scalars(select(ProductTag).where(ProductTag.product_id == product_id))}
            order = [row.key for row in fixed_fields(session)]
            order.extend(key for key in FIXED_FIELDS if key not in order)
            tag_rows = []
            for key in order:
                tag = tags.get(key)
                confirmed = tag is not None and tag.confirmation_status == "confirmed_value"
                tag_rows.append(TagRow(key=key, label=FIXED_FIELDS[key], value=tag.value if confirmed else "",
                    terms=tag.terms if confirmed else [], source=tag.source if tag else "",
                    confirmation_status=tag.confirmation_status if tag else "pending"))
            link = self.link_id(session, "product", product_id)
            return ProductDetail(id=product.id, product_code=product.product_code, display_name=product.display_name,
                kind=product.kind, status=product.status, free_text=product.free_text, file_id=file_id, tags=tag_rows,
                related=[self._brief(session, row, []) for row in related_rows], project=self._row(session, project),
                product_link=link, project_link=self.link_id(session, "project", project.id),
                product_url=self.url(link), ingested_at=product.ingested_at)

    def findings(self, login: LoginContext) -> list[AssetFinding]:
        with self.auth.database.session() as session:
            self.media._check_login(session, login)
            if "admin" not in session.get(User, login.user.id).roles:
                raise AppError("forbidden", "只有管理员可以查看文件巡检记录", 403)
            rows = session.scalars(select(InspectionFinding).where(InspectionFinding.run_id.is_(None))
                                   .order_by(InspectionFinding.id.desc()).limit(100))
            return [AssetFinding(id=row.id, project_id=row.project_id, file_id=row.file_id, kind=row.kind,
                                  detail=row.detail, created_at=row.created_at) for row in rows]
