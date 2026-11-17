"""入库后操作：改名、修改标签与关系、作废与恢复、移动、物理删除，以及补充包的落盘。

每种操作都是一个批次（batches.kind），参数存在 payload，复用两级审核；终审通过后由 postop 任务
以终审本人的 NAS 会话执行。物理删除例外：终审自己发起、自己执行，不经他人审核。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchFile, BatchProduct, CatalogFile, CompanyIndustryMap, HistoryVersion, Product, ProductRelation,
                           ProductTag, Project, TagField, User, utcnow)
from app.errors import AppError
from app.schemas.audit import ChangePreview, ChangeRow, IngestInfo
from app.schemas.postops import (MoveRequest, OpDetail, PurgeFile, PurgePreview, PurgeRequest, PurgeTargets, RenameRequest,
                                 RestoreRequest, VoidRequest)
from app.services import archive
from app.services.admin.company_map import WON_FOLDERS, company_path
from app.services.audit import AuditService
from app.services.auth.service import AuthService
from app.services.batch import BROKEN, DONE, LIGHT_KINDS, RUNNING, WITHDRAWN, kind_label, open_batch
from app.services.ingest import IngestService, steps_of
from app.services.nas import NasPermissionDenied, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import RESERVED
from app.services.review import ReviewService
from app.services.review.history import round_history
from app.services.search import SearchService
from app.services.thumbs import ThumbService
from app.services.upload import supplement
from .executor import Executor

UPLOAD_ROLES = {"uploader", "first_reviewer", "final_reviewer"}
VOIDED_ROOT = "_已作废项目"
STATUS_NAMES = {"active": "有效", "voided": "已作废", "deleted": "已删除", "replaced": "已被替换"}
WITHDRAWABLE = ("待核对", "已驳回", "待初审", "待终审")


def _name(value: str, what: str) -> str:
    name = value.strip()
    try:
        if not name or "/" in name or "\\" in name or safe_join(name) != name:
            raise NasPermissionDenied("名称非法")
    except NasPermissionDenied:
        raise AppError("invalid_name", f"{what}含非法字符", 422, {"name": value}) from None
    if name.casefold() in {item.casefold() for item in RESERVED}:
        raise AppError("invalid_name", f"{what}是系统保留名称", 422, {"name": value})
    return name


def _reason(value: str) -> str:
    text = value.strip()
    if not text:
        raise AppError("reason_required", "必须填写原因", 422)
    return text


class PostOpsService(Executor):
    def __init__(self, auth: AuthService, ingest: IngestService, review: ReviewService, audit: AuditService, search: SearchService,
                 thumbs: ThumbService) -> None:
        self.auth, self.ingest, self.review, self.search, self.thumbs = auth, ingest, review, search, thumbs
        self.paths = create_paths(auth.config)
        self.thumb_root = auth.config.paths.data_dir / "thumbs"
        # 仅供故障注入测试：执行到第几个文件操作后强制中断。
        self.fault_after_ops: int | None = None
        review.changes = self.retag_changes
        audit.preview = self.preview

    # ---- 发起前的共同检查 ----
    @staticmethod
    def _actor(session: Session, actor: User, final: bool = False) -> User:
        user = session.get(User, actor.id)
        if user is None or not user.enabled or not set(user.roles) & UPLOAD_ROLES:
            raise AppError("forbidden", "当前账号没有对已入库项目发起操作的权限", 403)
        if final and "final_reviewer" not in user.roles:
            raise AppError("forbidden", "只有终审可以发起这项操作", 403)
        return user

    @staticmethod
    def _project(session: Session, project_id: int, status: str = "active") -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AppError("project_not_found", "项目不存在", 404)
        if project.status != status:
            raise AppError("invalid_project_state", f"项目当前为「{STATUS_NAMES.get(project.status, project.status)}」，不能发起这项操作", 409)
        pending = open_batch(session, project)
        if pending is not None:
            raise AppError("project_busy", f"该项目有未完成的操作批次（{pending.batch_no}，{kind_label(pending)}），请等它结束或撤回后再发起", 409,
                           {"batch_id": pending.id, "kind": pending.kind, "status": pending.status})
        return project

    def _create(self, session: Session, project: Project, kind: str, actor: User, params: dict[str, object], host: str,
                status: str | None = None) -> Batch:
        single = kind in LIGHT_KINDS and self.auth.config.review.light_ops_single_review
        parent = project.current_path.rsplit("/", 1)[0] if "/" in project.current_path else project.current_path
        batch = Batch(batch_no=secrets.token_hex(16), project_id=project.id, project_code=project.project_code, kind=kind,
            status=status or ("待终审" if single else "待初审"), owner_id=actor.id, uploader_id=actor.id, temp_path="", won=project.won,
            recommended_path=parent, final_path=parent,
            payload={"folder_name": project.folder_name, "original_folder_name": project.folder_name, "params": params,
                     "stage_since": utcnow().isoformat()})
        session.add(batch)
        session.flush()
        batch.batch_no = f"B{utcnow():%Y%m%d}-{batch.id:06d}"
        if kind != "retag":
            # 参数类操作的预览在发起时就定下来，执行后卡片柜变了也能看到当时的改前改后。
            batch.payload = {**batch.payload, "preview": self._build_preview(session, batch).model_dump()}
        OplogService(session).write(actor.id, ",".join(actor.roles), f"发起{kind_label(batch)}", "batch", str(batch.id), None,
                                   {"project_id": project.id, "kind": kind, "status": batch.status, "params": params}, "成功", host)
        return batch

    # ---- 改名 ----
    def rename(self, project_id: int, body: RenameRequest, actor: User, host: str) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor)
            project = self._project(session, project_id)
            params: dict[str, object] = {"project_name": None, "products": []}
            if body.project_name is not None and body.project_name.strip() != project.project_name:
                name = _name(body.project_name, "项目名")
                if "_" in name:
                    raise AppError("invalid_name", "项目名不能包含下划线（文件夹名用下划线分成三段）", 422)
                parent = project.current_path.rsplit("/", 1)[0]
                folder = f"{project.project_code}_{project.company_name}_{name}"
                if self.auth.pool.get(user.id).exists(self.paths.join(Region.FORMAL, parent, folder)):
                    raise AppError("name_taken", "同一企业目录下已有同名的项目文件夹", 409)
                params["project_name"] = name
            files = {row.id: row for row in session.scalars(select(CatalogFile).where(CatalogFile.project_id == project.id, CatalogFile.status == "active"))}
            changes: dict[int, str] = {}
            for item in body.products:
                product = session.get(Product, item.id)
                if product is None or product.project_id != project.id or product.status != "active" or product.file_id not in files:
                    raise AppError("product_not_found", "要改名的商品不在该项目的有效商品中", 404)
                name = _name(item.name, "商品展示名")
                if name != product.display_name:
                    if name.casefold() == product.display_name.casefold():
                        raise AppError("invalid_name", "只改大小写的改名暂不支持", 422, {"name": name})
                    changes[product.id] = name
            # 保守处理：本次要改掉的旧名字也算占用（文件按顺序逐个改名，互换名字会在磁盘上相撞）。
            taken = {row.rel_path.casefold() for row in files.values()}
            for product_id, name in changes.items():
                file = files[session.get(Product, product_id).file_id]
                directory, old = file.rel_path.split("/", 1)
                target = f"{directory}/{name}.{old.rsplit('.', 1)[1]}"
                if target.casefold() in taken:
                    raise AppError("name_taken", f"项目内已有同名文件：{target}", 409, {"name": name})
                taken.add(target.casefold())
            params["products"] = [{"id": product_id, "name": name} for product_id, name in changes.items()]
            if params["project_name"] is None and not changes:
                raise AppError("no_changes", "没有任何改动", 422)
            return self._detail(session, self._create(session, project, "rename", user, params, host), user)

    # ---- 修改标签与关系 ----
    def retag(self, project_id: int, actor: User, host: str) -> OpDetail:
        """建立一个「待核对」的批次，把项目的有效商品带进核对页；提交后再走审核。"""
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor)
            project = self._project(session, project_id)
            batch = self._create(session, project, "retag", user, {}, host, status="待核对")
            fields = [{"key": row.key, "label": row.label, "hint": row.hint} for row in session.scalars(
                select(TagField).where(TagField.enabled.is_(True)).order_by(TagField.sort_order, TagField.id))]
            batch.payload = {**batch.payload, "ai_fields": fields}
            products = list(session.scalars(select(Product).where(Product.project_id == project.id, Product.status == "active").order_by(Product.product_code)))
            clones: dict[int, BatchProduct] = {}
            for product in products:
                source = session.get(CatalogFile, product.file_id)
                file = BatchFile(batch_id=batch.id, rel_path=source.rel_path, size=source.size, mtime=source.mtime, upload_status="done",
                                 bytes_received=source.size, fingerprint=source.fingerprint, thumb_status="done")
                session.add(file)
                session.flush()
                tags = {tag.field_key: {"value": tag.value, "terms": list(tag.terms), "source": tag.source, "ai_value": tag.ai_value, "flag": None}
                        for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == product.id))}
                clones[product.id] = BatchProduct(batch_id=batch.id, batch_file_id=file.id, kind=product.kind, display_name=product.display_name,
                    existing_product_id=product.id, product_code=product.product_code, ai_status="done", tags=tags, free_text=product.free_text,
                    free_text_source="ai", ai_free_text=product.free_text, relation_items=[], relation_confirmed=True, flags=[])
                session.add(clones[product.id])
            session.flush()
            for relation in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id.in_(list(clones)))) if clones else []:
                if relation.item_product_id in clones:
                    owner = clones[relation.set_product_id]
                    owner.relation_items = [*owner.relation_items, {"batch_product_id": clones[relation.item_product_id].id, "source": relation.source}]
            return self._detail(session, batch, user)

    def retag_changes(self, session: Session, batch: Batch) -> list[dict[str, object]]:
        """「修改标签」批次相对卡片柜的改动：逐字段、自由描述、套装包含的单品。"""
        labels = {row.key: row.label for row in session.scalars(select(TagField))}
        rows = [row for row in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id).order_by(BatchProduct.id)) if row.existing_product_id]
        targets = {row.id: row.existing_product_id for row in rows}
        changes: list[dict[str, object]] = []
        for row in rows:
            product = session.get(Product, row.existing_product_id)
            title = f"{product.product_code} {product.display_name}"
            current = {tag.field_key: tag.value for tag in session.scalars(select(ProductTag).where(ProductTag.product_id == product.id))}
            for key, tag in row.tags.items():
                value = str(tag.get("value", "")).strip() if isinstance(tag, dict) else ""
                if value != current.get(key, "").strip():
                    changes.append({"product_id": product.id, "type": "tag", "key": key, "label": f"{title} · {labels.get(key, key)}",
                                    "before": current.get(key, ""), "after": value})
            if row.free_text.strip() != product.free_text.strip():
                changes.append({"product_id": product.id, "type": "free_text", "label": f"{title} · 自由描述", "before": product.free_text, "after": row.free_text.strip()})
            if row.kind == "set":
                before = {relation.item_product_id for relation in session.scalars(select(ProductRelation).where(ProductRelation.set_product_id == product.id))}
                after = {targets[item["batch_product_id"]] for item in row.relation_items if isinstance(item, dict) and item.get("batch_product_id") in targets}
                if before != after:
                    names = lambda ids: "、".join(sorted(session.get(Product, value).display_name for value in ids)) or "（无）"  # noqa: E731
                    changes.append({"product_id": product.id, "type": "relations", "label": f"{title} · 包含的单品", "before": names(before), "after": names(after),
                                    "added": sorted(after - before), "removed": sorted(before - after)})
        return changes

    # ---- 作废与恢复 ----
    def void(self, project_id: int, body: VoidRequest, actor: User, host: str) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor)
            project = self._project(session, project_id)
            reason = _reason(body.reason)
            product_ids: list[int] = []
            for product_id in dict.fromkeys(body.product_ids):
                product = session.get(Product, product_id)
                if product is None or product.project_id != project.id or product.status != "active":
                    raise AppError("product_not_found", "要作废的商品不在该项目的有效商品中", 404)
                product_ids.append(product.id)
                if product.kind == "set" and body.cascade:
                    # 连同包含的单品一起作废。
                    for item_id in session.scalars(select(ProductRelation.item_product_id).where(ProductRelation.set_product_id == product.id)):
                        if session.get(Product, item_id).status == "active":
                            product_ids.append(item_id)
            product_ids = list(dict.fromkeys(product_ids))
            main_images = set(session.scalars(select(Product.file_id).where(Product.project_id == project.id)))
            file_ids: list[int] = []
            for file_id in dict.fromkeys(body.file_ids):
                file = session.get(CatalogFile, file_id)
                if file is None or file.project_id != project.id or file.status != "active":
                    raise AppError("file_not_found", "要作废的文件不在该项目的有效文件中", 404)
                if file.id in main_images:
                    raise AppError("invalid_target", "主图请按商品作废，不能单独作废图片文件", 422)
                file_ids.append(file.id)
            if not product_ids and not file_ids:
                raise AppError("no_changes", "请选择要作废的商品或资料文件", 422)
            params = {"action": "void", "scope": "items", "product_ids": product_ids, "file_ids": file_ids, "reason": reason, "cascade": body.cascade}
            return self._detail(session, self._create(session, project, "void", user, params, host), user)

    def void_project(self, project_id: int, reason: str, actor: User, host: str) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor, final=True)
            project = self._project(session, project_id)
            params = {"action": "void", "scope": "project", "reason": _reason(reason)}
            return self._detail(session, self._create(session, project, "void", user, params, host), user)

    def restore(self, project_id: int, body: RestoreRequest, actor: User, host: str) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor, final=True)
            reason = _reason(body.reason)
            if body.project:
                project = self._project(session, project_id, status="voided")
                if not project.lifecycle.get("original_path"):
                    raise AppError("invalid_project_state", "找不到该项目作废前的位置，无法恢复", 409)
                params: dict[str, object] = {"action": "restore", "scope": "project", "reason": reason}
                return self._detail(session, self._create(session, project, "void", user, params, host), user)
            project = self._project(session, project_id)
            occupied = set(supplement.catalog_files(session, project.id))
            file_ids: list[int] = []
            product_ids: list[int] = []
            for product_id in dict.fromkeys(body.product_ids):
                product = session.get(Product, product_id)
                if product is None or product.project_id != project.id or product.status != "voided":
                    raise AppError("product_not_found", "要恢复的商品不在该项目的已作废商品中", 404)
                product_ids.append(product.id)
                file_ids.append(product.file_id)
            for file_id in dict.fromkeys(body.file_ids):
                file = session.get(CatalogFile, file_id)
                if file is None or file.project_id != project.id or file.status != "voided":
                    raise AppError("file_not_found", "要恢复的文件不在该项目的已作废文件中", 404)
                if file.id not in file_ids:
                    file_ids.append(file.id)
            if not file_ids:
                raise AppError("no_changes", "请选择要恢复的商品或资料文件", 422)
            for file_id in file_ids:
                row = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file_id, HistoryVersion.reason == "voided").order_by(HistoryVersion.id.desc()))
                if row is None:
                    raise AppError("invalid_target", "找不到作废前的位置，无法恢复", 409)
                if row.old_rel_path.casefold() in occupied:
                    raise AppError("name_taken", f"原位置已有同名文件：{row.old_rel_path}", 409)
                occupied.add(row.old_rel_path.casefold())
            params = {"action": "restore", "scope": "items", "product_ids": product_ids, "file_ids": file_ids, "reason": reason}
            return self._detail(session, self._create(session, project, "void", user, params, host), user)

    # ---- 移动 ----
    def move(self, project_id: int, body: MoveRequest, actor: User, host: str) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor, final=True)
            project = self._project(session, project_id)
            company = session.get(CompanyIndustryMap, body.company_id)
            if company is None:
                raise AppError("company_not_found", "只能移到现有目录树中的位置", 422)
            target = company_path(body.won, company)
            if target == project.current_path.rsplit("/", 1)[0]:
                raise AppError("no_changes", "项目已经在这个位置", 422)
            if self.auth.pool.get(user.id).exists(self.paths.join(Region.FORMAL, target, project.folder_name)):
                raise AppError("name_taken", "目标位置已有同名的项目文件夹", 409)
            params = {"won": body.won, "company_id": company.id, "target": target, "reason": _reason(body.reason)}
            return self._detail(session, self._create(session, project, "move", user, params, host), user)

    # ---- 物理删除 ----
    def _purge_targets(self, session: Session, project: Project, body: PurgeTargets) -> tuple[list[CatalogFile], list[Product], list[datetime], list[str]]:
        files: list[CatalogFile] = []
        products: list[Product] = []
        voided: list[datetime] = []
        summary: list[str] = []

        def moment(raw: object, fallback: datetime) -> datetime:
            value = datetime.fromisoformat(raw) if isinstance(raw, str) else fallback
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        if body.project:
            if body.history_file_ids or body.product_ids:
                raise AppError("invalid_target", "删除整个项目时不能同时选择其他内容", 422)
            if project.status != "voided":
                raise AppError("invalid_target", "只能删除已作废的内容：该项目尚未作废", 409)
            files = [row for row in session.scalars(select(CatalogFile).where(CatalogFile.project_id == project.id).order_by(CatalogFile.rel_path)) if row.status != "deleted"]
            products = [row for row in session.scalars(select(Product).where(Product.project_id == project.id)) if row.status != "deleted"]
            voided.append(moment(project.lifecycle.get("voided_at"), project.updated_at))
            summary.append(f"整个项目 {project.folder_name}（{len(products)} 个商品、{len(files)} 个文件，连同档案）")
            return files, products, voided, summary
        if project.status != "active":
            raise AppError("invalid_target", "项目已作废：请恢复后再删除其中的内容，或直接删除整个项目", 409)
        for product_id in dict.fromkeys(body.product_ids):
            product = session.get(Product, product_id)
            if product is None or product.project_id != project.id:
                raise AppError("product_not_found", "商品不在该项目中", 404)
            if product.status != "voided":
                raise AppError("invalid_target", f"只能删除已作废的内容：商品 {product.product_code} 不是已作废状态", 409)
            products.append(product)
            files.append(session.get(CatalogFile, product.file_id))
            voided.append(moment(product.lifecycle.get("voided_at"), product.updated_at))
            summary.append(f"已作废商品 {product.product_code} {product.display_name}（主图和档案）")
        owned = {row.file_id for row in session.scalars(select(Product).where(Product.project_id == project.id))}
        for file_id in dict.fromkeys(body.history_file_ids):
            file = session.get(CatalogFile, file_id)
            if file is None or file.project_id != project.id:
                raise AppError("file_not_found", "文件不在该项目中", 404)
            if file.status not in ("voided", "replaced"):
                raise AppError("invalid_target", f"只能删除已作废的内容：{file.rel_path} 仍是有效文件", 409)
            if file.status == "voided" and file.id in owned:
                raise AppError("invalid_target", "已作废商品的主图请按商品删除", 422)
            row = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file.id).order_by(HistoryVersion.id.desc()))
            if file not in files:
                files.append(file)
            voided.append(moment(None, row.created_at if row else file.updated_at))
            summary.append(f"历史版本文件 {file.rel_path}")
        if not files:
            raise AppError("no_changes", "请选择要删除的内容", 422)
        return files, products, voided, summary

    def _purge_preview(self, session: Session, project: Project, body: PurgeTargets) -> tuple[PurgePreview, str, list[CatalogFile], list[Product]]:
        files, products, voided, summary = self._purge_targets(session, project, body)
        days = self.auth.config.purge.cooling_days
        allowed = max(voided) + timedelta(days=days)
        listing = [PurgeFile(path=f"{project.current_path}/{row.rel_path}", size=row.size) for row in files]
        if body.project:
            listing += [PurgeFile(path=f"{project.current_path}/{row.archive_rel_path}", size=0) for row in products]
            listing.append(PurgeFile(path=f"{project.current_path}/{archive.PROJECT_ARCHIVE}", size=0))
        else:
            listing += [PurgeFile(path=f"{project.current_path}/{row.archive_rel_path}", size=0) for row in products]
        single = len(products) == 1 and not body.project and not body.history_file_ids
        code = products[0].product_code if single else project.project_code
        preview = PurgePreview(files=listing, count=len(listing), total_size=sum(item.size for item in listing),
            confirm_label="商品编号" if single else "立项编号", cooling_days=days, in_cooling=utcnow() < allowed, earliest_allowed=allowed, summary=summary)
        return preview, code, files, products

    def purge_preview(self, project_id: int, body: PurgeTargets, actor: User) -> PurgePreview:
        with self.auth.lock, self.auth.database.session() as session:
            self._actor(session, actor, final=True)
            project = self._project(session, project_id, status="voided" if body.project else "active")
            return self._purge_preview(session, project, body)[0]

    def purge(self, project_id: int, body: PurgeRequest, actor: User, host: str) -> OpDetail:
        """终审自己发起并执行，不经他人审核；直接进入执行。"""
        with self.auth.lock, self.auth.database.session() as session:
            user = self._actor(session, actor, final=True)
            project = self._project(session, project_id, status="voided" if body.project else "active")
            preview, code, files, products = self._purge_preview(session, project, body)
            reason = _reason(body.reason)
            exception = body.exception_reason.strip()
            if preview.in_cooling and not exception:
                raise AppError("cooling_period", f"作废未满 {preview.cooling_days} 天，需要填写例外原因才能删除", 409,
                               {"earliest_allowed": preview.earliest_allowed.isoformat() if preview.earliest_allowed else None})
            if body.confirm.strip().upper() != code.upper():
                raise AppError("confirm_mismatch", f"输入的{preview.confirm_label}不正确，未执行删除", 422)
            params = {"project": body.project, "file_ids": [row.id for row in files], "product_ids": [row.id for row in products],
                      "reason": reason, "exception_reason": exception if preview.in_cooling else "", "skipped_cooling": preview.in_cooling,
                      "listing": [item.model_dump() for item in preview.files], "summary": preview.summary}
            batch = self._create(session, project, "purge", user, params, host, status=RUNNING)
            batch.final_reviewer_id = user.id
            self.ingest.enqueue(session, batch, user)
            return self._detail(session, batch, user)

    # ---- 变更预览 ----
    def _build_preview(self, session: Session, batch: Batch) -> ChangePreview:
        project = session.get(Project, batch.project_id)
        params = batch.payload.get("params", {})
        preview = ChangePreview(kind=batch.kind, kind_label=kind_label(batch), reason=params.get("reason") if isinstance(params, dict) else None)
        title = lambda product: f"{product.product_code} {product.display_name}"  # noqa: E731
        if batch.kind == "rename":
            if params.get("project_name"):
                folder = f"{project.project_code}_{project.company_name}_{params['project_name']}"
                preview.rows.append(ChangeRow(label="项目名", before=project.project_name, after=str(params["project_name"])))
                preview.rows.append(ChangeRow(label="项目文件夹", before=project.folder_name, after=folder))
            for item in params.get("products", []):
                product = session.get(Product, item["id"])
                file = session.get(CatalogFile, product.file_id)
                directory, old = file.rel_path.split("/", 1)
                target = f"{directory}/{item['name']}.{old.rsplit('.', 1)[1]}"
                preview.rows.append(ChangeRow(label=f"{product.product_code} 展示名", before=product.display_name, after=item["name"]))
                preview.products.append(title(product))
                preview.files.append(f"{file.rel_path} → {target}")
            preview.notes.append("商品编号和永久链接不变；三层档案随之更新。")
        elif batch.kind == "retag":
            for change in self.retag_changes(session, batch):
                preview.rows.append(ChangeRow(label=str(change["label"]), before=str(change["before"]), after=str(change["after"])))
            preview.products = list(dict.fromkeys(row.label.split(" · ")[0] for row in preview.rows))
            preview.notes.append("只更新卡片柜和档案，不动图片。")
        elif batch.kind == "void" and params.get("scope") == "project":
            restore = params.get("action") == "restore"
            target = str(project.lifecycle.get("original_path", "")) if restore else f"{VOIDED_ROOT}/{project.folder_name}"
            preview.rows.append(ChangeRow(label="项目位置", before=project.current_path, after=target))
            preview.rows.append(ChangeRow(label="项目状态", before=STATUS_NAMES[project.status], after="有效" if restore else "已作废"))
            products = [row for row in session.scalars(select(Product).where(Product.project_id == project.id).order_by(Product.product_code))
                        if (row.status == "voided" and row.lifecycle.get("voided_by") == "project") or (not restore and row.status == "active")]
            preview.products = [title(row) for row in products]
            preview.notes.append("整个项目文件夹移回原位置，随项目一起作废的商品恢复为有效。" if restore else "整个项目文件夹移到「_已作废项目」，全部商品作废，从搜索中消失。")
        elif batch.kind == "void":
            restore = params.get("action") == "restore"
            for product_id in params.get("product_ids", []):
                product = session.get(Product, product_id)
                preview.products.append(title(product))
                preview.rows.append(ChangeRow(label=f"{title(product)} 状态", before=STATUS_NAMES[product.status], after="有效" if restore else "已作废"))
                if not restore and product.kind == "set":
                    count = len(list(session.scalars(select(ProductRelation.id).where(ProductRelation.set_product_id == product.id))))
                    preview.notes.append(f"套装 {product.display_name} 与 {count} 个单品的包含关系随之失效" + ("，这些单品一并作废。" if params.get("cascade") else "，单品本身不受影响。"))
            owned = {session.get(Product, value).file_id for value in params.get("product_ids", [])}
            for file_id in [*owned, *params.get("file_ids", [])]:
                file = session.get(CatalogFile, file_id)
                if restore:
                    row = session.scalar(select(HistoryVersion).where(HistoryVersion.file_id == file.id, HistoryVersion.reason == "voided").order_by(HistoryVersion.id.desc()))
                    line = f"{file.rel_path} → {row.old_rel_path if row else '原位置'}"
                else:
                    line = f"{file.rel_path} → {archive.HISTORY_DIR}/"
                if line not in preview.files:
                    preview.files.append(line)
        elif batch.kind == "move":
            company = session.get(CompanyIndustryMap, params["company_id"])
            preview.rows.append(ChangeRow(label="项目位置", before=project.current_path.rsplit("/", 1)[0], after=str(params["target"])))
            preview.rows.append(ChangeRow(label="中标状态", before="已中标" if project.won else "未中标", after="已中标" if params["won"] else "未中标"))
            preview.rows.append(ChangeRow(label="企业", before=project.company_name, after=company.company_name if company else ""))
            preview.notes.append("移动整个项目文件夹；商品编号和永久链接不变。")
        elif batch.kind == "purge":
            preview.rows.append(ChangeRow(label="删除文件数", before=str(len(params["listing"])), after="0"))
            preview.rows.append(ChangeRow(label="是否跳过冷静期", before="—", after=f"是（例外原因：{params['exception_reason']}）" if params.get("skipped_cooling") else "否"))
            preview.files = [f"{item['path']}（{item['size']} 字节）" for item in params["listing"]]
            preview.notes = [*params.get("summary", []), "删除后卡片柜保留记录并标记为已删除。"]
        elif batch.kind == "supplement":
            files = [row for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.rel_path)) if not row.removed]
            counts = {"新增": sum(row.conflict == "new" for row in files), "替换": sum(row.conflict_choice == "replace" for row in files),
                      "另存为新文件": sum(row.conflict_choice == "keep_both" for row in files), "跳过（与库中相同）": sum(row.conflict == "duplicate" for row in files)}
            preview.rows = [ChangeRow(label=name, before="—", after=f"{count} 个") for name, count in counts.items()]
            marks = {"new": "新增", "duplicate": "跳过"}
            preview.files = [f"{marks.get(row.conflict or '', '替换' if row.conflict_choice == 'replace' else '另存')}：{row.rel_path}" for row in files]
            preview.products = [f"{'替换主图' if row.existing_product_id else '新商品'}：{row.display_name}" for row in session.scalars(
                select(BatchProduct).join(BatchFile, BatchFile.id == BatchProduct.batch_file_id).where(BatchProduct.batch_id == batch.id, BatchFile.removed.is_not(True)))]
            preview.notes.append("被替换的旧文件移入「_历史版本」，文件名加日期；被替换主图的商品编号不变。")
        return preview

    def preview(self, session: Session, batch: Batch) -> ChangePreview | None:
        if batch.kind == "ingest" or batch.project_id is None:
            return None
        stored = batch.payload.get("preview")
        if isinstance(stored, dict):
            return ChangePreview.model_validate(stored)
        return self._build_preview(session, batch)

    # ---- 查看与撤回 ----
    def _detail(self, session: Session, batch: Batch, user: User) -> OpDetail:
        session.flush()
        owner = session.get(User, batch.owner_id)
        names = steps_of(batch.kind)
        progress = IngestInfo(step=batch.ingest_step, total=len(names), error=batch.ingest_error,
            current=names.get(batch.ingest_step + 1) if batch.status in (RUNNING, BROKEN) else None,
            can_resume=batch.status == BROKEN and batch.final_reviewer_id == user.id and "final_reviewer" in user.roles) if batch.status in (RUNNING, BROKEN, DONE) else None
        return OpDetail(id=batch.id, batch_no=batch.batch_no, kind=batch.kind, kind_label=kind_label(batch), status=batch.status,
            project_id=batch.project_id, project_code=batch.project_code, folder_name=str(batch.payload.get("folder_name", "")),
            owner_id=batch.owner_id, owner_name=(owner.display_name or owner.username) if owner else "", round=batch.round,
            created_at=batch.created_at, preview=self.preview(session, batch), progress=progress,
            can_withdraw=batch.kind != "ingest" and batch.status in WITHDRAWABLE and ("admin" in user.roles or user.id == batch.owner_id),
            history=round_history(session, batch.id))

    def detail(self, batch_id: int, actor: User) -> OpDetail:
        with self.auth.lock, self.auth.database.session() as session:
            user = session.get(User, actor.id)
            batch = session.get(Batch, batch_id)
            if batch is None or user is None or not user.enabled:
                raise AppError("batch_not_found", "批次不存在", 404)
            return self._detail(session, batch, user)

    def withdraw(self, batch_id: int, actor: User, host: str) -> OpDetail:
        """任何未通过终审的操作批次都可由责任人撤回；补充批次同时删除临时区里的文件。"""
        with self.auth.lock, self.auth.database.session() as session:
            user = session.get(User, actor.id)
            batch = session.get(Batch, batch_id)
            if batch is None or user is None or not user.enabled:
                raise AppError("batch_not_found", "批次不存在", 404)
            if batch.kind == "ingest":
                raise AppError("not_supported", "首次入库的批次请在批次页面撤回", 409)
            if "admin" not in user.roles and user.id != batch.owner_id:
                raise AppError("forbidden", "只有责任人和管理员可以撤回", 403)
            if batch.status not in WITHDRAWABLE:
                raise AppError("invalid_batch_state", f"批次当前为「{batch.status}」，不能撤回", 409)
            before = {"status": batch.status}
            if batch.kind == "supplement":
                fs = self.auth.pool.get(user.id)
                root = self.paths.join(Region.TEMP, batch.batch_no)
                if fs.exists(root):
                    fs.delete(root, recursive=True)
                self.thumbs.invalidate(batch.id)
            batch.status = WITHDRAWN
            batch.first_reviewer_id = batch.final_reviewer_id = None
            OplogService(session).write(user.id, ",".join(user.roles), f"撤回{kind_label(batch)}", "batch", str(batch.id), before, {"status": WITHDRAWN}, "成功", host)
            return self._detail(session, batch, user)
