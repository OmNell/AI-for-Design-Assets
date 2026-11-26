"""入库后变更的草稿、差异预览及双审快照。"""
from __future__ import annotations

import secrets

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, CatalogFile, CompanyIndustryMap, Product, Project, ReviewSnapshot, User, utcnow
from app.errors import AppError
from app.schemas.postops import (LightKind, MoveParams, OperationBody, OperationFile, OperationKind, OperationResponse,
                                 ProjectOperations, RenameParams, RetagParams, SupplementParams, TargetParams, OperationModel)
from app.schemas.review import ReviewResponse
from app.schemas.upload import PrecheckRequest, PrecheckResponse
from app.services import archive
from app.services.archive import repair
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.ai.orchestrator import REVIEWING, active_products
from app.services.auth.service import LoginContext
from app.services.nas import NasNotFound, NasPermissionDenied, Region, safe_join
from app.services.review import REJECTED, SUBMITTED, UPLOAD_ROLES, ReviewService
from app.services.thumbs import is_main_image
from app.services.upload.service import STAGED, UploadService
from .manifest import digest, prevalidate, purge_preview
from .supplement import active_batch_id, catalog_files, project_for_supplement

DRAFT = "待核对"
LIGHT_KINDS = {"rename", "retag", "void", "restore", "move", "purge", "archive_repair"}
PARAMS = {"rename": RenameParams, "retag": RetagParams, "move": MoveParams, "void": TargetParams,
          "restore": TargetParams, "purge": TargetParams, "supplement": SupplementParams, "archive_repair": OperationModel}


class DraftService:
    def __init__(self, upload: UploadService, review: ReviewService) -> None:
        self.upload, self.review, self.auth = upload, review, upload.auth
        self.review.operation_snapshot = self.supplement_snapshot

    def _project(self, session: Session, project_id: int, login: LoginContext) -> tuple[Project, User]:
        user = self.review._user(session, login.user)
        self.upload._check_login(session, login)
        project = session.get(Project, project_id)
        if project is None or not project.current_path or project.status not in {"active", "voided", "deleted"}:
            raise AppError("project_not_found", "正式项目不存在", 404)
        fs = self.auth.pool.get(login.session_id)
        if not fs.stat(self.upload.paths.join(Region.FORMAL, project.current_path)).is_dir:
            raise NasNotFound("正式项目目录不存在")
        return project, user

    @staticmethod
    def _kinds(project: Project, user: User) -> list[OperationKind]:
        if project.status == "deleted":
            return ["archive_repair"] if "final_reviewer" in user.roles else []
        if not set(user.roles) & UPLOAD_ROLES:
            return []
        kinds: list[OperationKind] = ["supplement", "rename", "retag", "void"] if project.status == "active" else []
        if "final_reviewer" in user.roles:
            kinds.extend(["restore", "purge", "archive_repair"])
            if project.status == "active":
                kinds.append("move")
        return kinds

    def options(self, project_id: int, login: LoginContext) -> ProjectOperations:
        with self.auth.database.session() as session:
            project, user = self._project(session, project_id, login)
            active = active_batch_id(session, project.id)
            batch = session.get(Batch, active) if active is not None else None
            return ProjectOperations(project_id=project.id, project_code=project.project_code,
                folder_name=project.folder_name, status=project.status, before=archive.from_catalog(session, project),
                kinds=self._kinds(project, user), active_batch_id=active if batch is not None and batch.owner_id == user.id else None)

    def voided(self, login: LoginContext) -> list[ProjectOperations]:
        with self.auth.database.session() as session:
            ids = list(session.scalars(select(Project.id).where(Project.status == "voided").order_by(Project.project_code)))
        result = []
        for project_id in ids:
            try:
                result.append(self.options(project_id, login))
            except (NasPermissionDenied, NasNotFound):
                continue
        return result

    def precheck(self, project_id: int, body: PrecheckRequest, login: LoginContext) -> PrecheckResponse:
        with self.auth.database.session() as session:
            project, user = self._project(session, project_id, login)
            if "supplement" not in self._kinds(project, user):
                raise AppError("forbidden", "当前不能为该项目发起补充", 403)
            return self.upload.precheck(body, project=project)

    @staticmethod
    def _parse(kind: str, params: dict, *, strict: bool = False) -> dict:
        if not params and not strict and kind != "supplement":
            return {}
        try:
            model = PARAMS[kind].model_validate(params)
        except ValidationError as exc:
            raise AppError("invalid_operation", "操作参数不符合要求", 422,
                           exc.errors(include_context=False, include_input=False, include_url=False)) from exc
        return model.model_dump(mode="json", exclude_none=True)

    def _review_permission(self, project: Project, batch: Batch, user: User, params: dict) -> None:
        if batch.kind not in self._kinds(project, user):
            raise AppError("forbidden", "当前账号不能发起此项目操作", 403)
        if batch.kind == "void" and params.get("project") and "final_reviewer" not in user.roles:
            raise AppError("forbidden", "只有终审角色可以发起整项目作废", 403)

    @staticmethod
    def _name(value: str, *, component: bool) -> None:
        try:
            if safe_join(value) != value or "/" in value or component and "_" in value:
                raise NasPermissionDenied("名称必须是单独的路径片段")
        except NasPermissionDenied as exc:
            raise AppError("invalid_name", "名称含非法字符或分隔符", 422) from exc

    def _validate_light(self, session: Session, batch: Batch, project: Project, user: User, params: dict) -> None:
        self._review_permission(project, batch, user, params)
        if not params:
            return
        members = {row.id: row for row in session.scalars(
            select(Product).where(Product.project_id == project.id, Product.status == "active"))}
        if batch.kind in {"rename", "retag"}:
            product_ids = [row["id"] for row in params["products"]]
            if set(product_ids) - members.keys() or len(product_ids) != len(set(product_ids)):
                raise AppError("invalid_products", "商品不存在、已作废或重复", 422)
        if batch.kind == "rename":
            if project_name := params.get("project_name"):
                self._name(project_name, component=True)
            for product in params["products"]:
                if product["rename_file"]:
                    self._name(product["name"], component=False)
        if batch.kind == "retag":
            for product in params["products"]:
                for key, tag in product["tags"].items():
                    if key not in FIXED_FIELDS or (tag["confirmation_status"] == "confirmed_value") != bool(tag["value"]):
                        raise AppError("invalid_tag", "请使用固定标签并明确确认字段值或无法判断", 422, {"field": key})
                explicit_items = product.get("item_ids")
                if explicit_items is not None:
                    potential = {key for key, row in members.items() if row.kind == "item"}
                    if members[product["id"]].kind != "set" or set(explicit_items) - potential:
                        raise AppError("invalid_relation", "套装只能关联本项目内的有效单品", 422)
        if batch.kind == "move" and session.get(CompanyIndustryMap, params["company_id"]) is None:
            raise AppError("invalid_company", "请选择目录树中的有效企业", 422)

    @staticmethod
    def _locked(batch: Batch, user: User, *, staged: bool = False) -> None:
        if batch.owner_id != user.id or not set(user.roles) & UPLOAD_ROLES:
            raise AppError("forbidden", "只有当前批次责任人可以修改", 403)
        editable_states = (*REVIEWING, STAGED) if staged else REVIEWING
        if batch.status not in editable_states:
            raise AppError("invalid_batch_state", "批次当前状态不能修改，已提交内容已冻结", 409)

    def _batch(self, session: Session, batch_id: int, login: LoginContext) -> tuple[Batch, Project, User]:
        batch, user = self.review._readable(session, batch_id, login.user)
        if batch.kind not in PARAMS or batch.project_id is None:
            raise AppError("operation_not_found", "入库后操作批次不存在", 404)
        project, user = self._project(session, batch.project_id, login)
        return batch, project, user

    @staticmethod
    def _changes(batch: Batch) -> dict:
        return dict(batch.payload.get("operation", {}))

    @staticmethod
    def _freeze(session: Session, project: Project, params: dict) -> dict:
        before = archive.from_catalog(session, project)
        return {"params": params, "before": before, "before_digest": digest(before),
                "physical_paths": {str(row.id): row.physical_rel_path for row in session.scalars(
                    select(CatalogFile).where(CatalogFile.project_id == project.id))}}

    @staticmethod
    def _difference(plan: dict) -> dict:
        """只展示有业务意义的值，正式身份和链接由发布事务分配。"""
        def index(manifest: dict) -> dict[str, dict]:
            products = {row["id"]: row["product_code"] for row in manifest["products"]}
            files = {row["id"]: row["rel_path"] for row in manifest["files"]}
            return {
                "project": {"project": {key: manifest["project"].get(key) for key in (
                    "project_name", "folder_name", "company_name", "company_id", "industry_id", "won", "status", "canonical_path")}},
                "files": {str(row["id"]): {key: row[key] for key in ("rel_path", "fingerprint", "size", "mtime", "status")}
                          for row in manifest["files"]},
                "products": {row["product_code"]: {**{key: row[key] for key in ("display_name", "kind", "status", "free_text")},
                    "rel_path": files[row["file_id"]]} for row in manifest["products"]},
                "tags": {f"{products[row['product_id']]}:{row['field_key']}": {
                    key: row.get(key) for key in ("value", "source", "confirmation_status")} for row in manifest["tags"]},
                "relations": {f"{products[row['set_product_id']]}:{products[row['item_product_id']]}": row["status"]
                              for row in manifest["relations"]},
            }
        baseline = plan["baseline"]
        original = index(baseline)
        known_files = {row["id"] for row in baseline["files"]}
        proposed_manifest = plan["manifest"]
        proposed = index(proposed_manifest)
        for row in proposed_manifest["files"]:
            if row["id"] not in known_files:
                proposed["files"][row["rel_path"]] = proposed["files"].pop(str(row["id"]))
        result = {}
        for name, before in original.items():
            after = proposed[name]
            result[name] = {key: {"before": before.get(key), "after": after.get(key)}
                            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)}
        return result

    def _response(self, session: Session, batch: Batch, project: Project, user: User, *, preview: bool = False) -> OperationResponse:
        params = self._changes(batch)
        operation = self._freeze(session, project, params)
        if batch.review_snapshot_hash and batch.status not in REVIEWING:
            operation = self.review.submitted_snapshot(session, batch).content["operation"]
            params = operation["params"]
        conflicts = []
        required = []
        if batch.kind == "supplement":
            current = {f["rel_path"].casefold(): f for f in operation["before"]["files"] if f["status"] == "active"}
            for f in session.scalars(select(BatchFile).where(
                    BatchFile.batch_id == batch.id, BatchFile.removed.is_not(True)).order_by(BatchFile.rel_path)):
                candidate = current.get(f.rel_path.casefold())
                conflicts.append(OperationFile(id=f.id, rel_path=f.rel_path, size=f.size, mtime=f.mtime, fingerprint=f.fingerprint,
                    conflict=f.conflict, choice=f.conflict_choice, file_id=candidate["id"] if candidate else None))
            required = self._required_relations(session, batch, operation["before"])
        purge = operation.get("purge")
        after = operation.get("after", {})
        if preview and batch.kind == "purge" and params and purge is None:
            purge = purge_preview(session, project, params)
        if preview and batch.kind in LIGHT_KINDS - {"purge"} and params and "after" not in operation:
            content = {"submitted_by": user.id, "operation": operation, "files": []}
            after = self._difference(prevalidate(session, batch, content))
        return OperationResponse(id=batch.id, batch_id=batch.id, project_id=project.id, kind=batch.kind,
            status=batch.status, editable=user.id == batch.owner_id and (
                batch.status in REVIEWING or batch.kind == "supplement" and batch.status == STAGED),
            params=params, before=operation["before"], after=after, conflicts=conflicts,
            purge=purge, required_relation_reviews=required)

    def read(self, batch_id: int, login: LoginContext, *, preview: bool = False) -> OperationResponse:
        with self.auth.database.session() as session:
            batch, project, user = self._batch(session, batch_id, login)
            response = self._response(session, batch, project, user, preview=preview)
            if preview and batch.kind == "archive_repair" and response.editable:
                fs = self.auth.pool.get(login.session_id)
                response.after = {"files": repair.changes(fs, self.upload.paths.join(Region.FORMAL, project.current_path), response.before)}
            return response

    def create(self, project_id: int, kind: LightKind, body: OperationBody, login: LoginContext, host: str) -> OperationResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            project, user = self._project(session, project_id, login)
            if kind not in self._kinds(project, user):
                raise AppError("forbidden", "当前账号不能发起此项目操作", 403)
            if active_batch_id(session, project.id) is not None:
                raise AppError("project_busy", "此项目已有未完成的操作批次", 409)
            params = self._parse(kind, body.params)
            batch = Batch(batch_no=secrets.token_hex(16), project_code=project.project_code, kind=kind, status=DRAFT,
                project_id=project.id, owner_id=user.id, uploader_id=user.id, upload_session_id=login.session_id,
                temp_path="", won=project.won, recommended_path=project.current_path, note_to_reviewer=body.note or None,
                payload={"folder_name": project.folder_name, "original_folder_name": project.folder_name,
                         "directories": [], "operation": params, "stage_since": utcnow().isoformat()})
            self._validate_light(session, batch, project, user, params)
            session.add(batch)
            session.flush()
            batch.batch_no = f"B{utcnow():%Y%m%d}-{batch.id:06d}"
            self.review._log(session, user, "创建入库后操作草稿", "batch", batch.id, None,
                             {"kind": kind, "project_id": project.id, "params": params}, host)
            return self._response(session, batch, project, user)

    def update(self, batch_id: int, body: OperationBody, login: LoginContext, host: str) -> OperationResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, project, user = self._batch(session, batch_id, login)
            self._locked(batch, user, staged=batch.kind == "supplement")
            before = self._changes(batch)
            if batch.kind == "supplement":
                params = self._parse(batch.kind, {**before, **body.params})
                if params["products"] != before.get("products", []):
                    # 更换主图映射后，已有关系确认只在本请求重新显式提交时保留。
                    for key in ("relation_reviews", "relation_batch_items", "batch_set_items"):
                        if key not in body.params:
                            params[key] = {}
                self._apply_supplement(session, batch, project, user, params, host)
            else:
                params = self._parse(batch.kind, body.params)
                self._validate_light(session, batch, project, user, params)
            batch.payload = {**batch.payload, "operation": params}
            batch.note_to_reviewer = body.note or None
            batch.updated_at = utcnow()
            self.review._log(session, user, "更新入库后操作草稿", "batch", batch.id, before, params, host)
            return self._response(session, batch, project, user)

    def submit(self, batch_id: int, body: OperationBody, login: LoginContext, host: str) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, project, user = self._batch(session, batch_id, login)
            self._locked(batch, user)
            if batch.kind not in LIGHT_KINDS:
                raise AppError("invalid_operation", "补充批次请从原核对页面提交", 422)
            params = self._parse(batch.kind, body.params or self._changes(batch), strict=True)
            self._validate_light(session, batch, project, user, params)
            operation = self._freeze(session, project, params)
            if batch.kind == "purge":
                operation["purge"] = purge_preview(session, project, params)
                if not operation["purge"]["eligible"] or params.get("confirm") != operation["purge"]["confirm_label"]:
                    raise AppError("purge_confirmation_required", "删除清单须满足冷静期并准确输入确认编号", 409,
                                   operation["purge"])
            view = self.review._response(session, batch, user)
            view.batch.round += int(batch.status == REJECTED)
            view.batch.status, view.batch.editable = SUBMITTED, False
            view.batch.note_to_reviewer, view.batch.review_snapshot_hash = body.note or None, None
            view.pending.can_submit = False
            content = {"schema": 1, "submitted_by": user.id, "kind": batch.kind, "project_id": project.id,
                       "review": view.model_dump(mode="json"), "evidence": {}, "confirmation_oplog_ids": [],
                       "directories": [], "preflight_acks": [], "files": [], "operation": operation}
            if batch.kind == "archive_repair":
                fs = self.auth.pool.get(login.session_id)
                root = self.upload.paths.join(Region.FORMAL, project.current_path)
                scope = repair.changes(fs, root, operation["before"])
                if all(entry["before"] == entry["after"] for entry in scope.values()):
                    try:
                        if not archive.verify(session, project, fs, root):
                            raise AppError("archive_intact", "档案完整，无需发起修复", 409)
                    except archive.ArchiveError:
                        pass
                operation["repair"] = scope
                operation["after"] = {"files": scope}
            else:
                operation["after"] = self._difference(prevalidate(session, batch, content))
            digest = self.review._digest(content)
            before = {"status": batch.status, "round": batch.round}
            batch.round, batch.note_to_reviewer = view.batch.round, view.batch.note_to_reviewer
            batch.status, batch.review_snapshot_hash = SUBMITTED, digest
            batch.payload = {**{key: value for key, value in batch.payload.items() if key not in ("review_location", "claim_nonce")},
                             "operation": params, "stage_since": utcnow().isoformat()}
            batch.final_path = None
            batch.first_reviewer_id = batch.final_reviewer_id = None
            session.add(ReviewSnapshot(batch_id=batch.id, round=batch.round, submitted_by=user.id, digest=digest, content=content))
            self.review._log(session, user, "提交入库后操作审核", "batch", batch.id, before,
                {"status": SUBMITTED, "round": batch.round, "review_snapshot_hash": digest, "params": params}, host)
            view.batch.review_snapshot_hash = digest
            return view

    @staticmethod
    def _required_relations(session: Session, batch: Batch, before: archive.Manifest) -> list[int]:
        products = active_products(session, batch.id)
        replacement_items = {p.existing_product_id for p in products if p.kind == "item" and p.existing_product_id is not None}
        replacement_sets = {p.existing_product_id for p in products if p.kind == "set" and p.existing_product_id is not None}
        active_sets = {p["id"] for p in before["products"] if p["kind"] == "set" and p["status"] == "active"}
        affected = {r["set_product_id"] for r in before["relations"] if r["status"] == "active"
                    and r["item_product_id"] in replacement_items}
        return sorted((active_sets if any(p.kind == "item" and p.existing_product_id is None for p in products)
                       else active_sets & affected) - replacement_sets)

    def _apply_supplement(self, session: Session, batch: Batch, project: Project, user: User, params: dict, host: str) -> None:
        self._review_permission(project, batch, user, params)
        files = {row.id: row for row in session.scalars(select(BatchFile).where(
            BatchFile.batch_id == batch.id, BatchFile.removed.is_not(True), BatchFile.conflict == "conflict"))}
        choices = {row["id"]: row["choice"] for row in params["files"]}
        if len(choices) != len(params["files"]) or choices.keys() - files.keys():
            raise AppError("invalid_conflict_choice", "只能处置本批次发生冲突的文件", 422)
        for file in files.values():
            choice = choices.get(file.id)
            if choice != file.conflict_choice:
                self.review._log(session, user, "选择补充文件冲突处理", "batch_file", file.id,
                    {"choice": file.conflict_choice}, {"choice": choice}, host)
                file.conflict_choice = choice
        above = archive.from_catalog(session, project)
        formal = {p["id"]: p for p in above["products"] if p["status"] == "active"}
        products = {p.id: p for p in active_products(session, batch.id)}
        assignments = {row["id"]: row.get("existing_product_id") for row in params["products"]}
        targets = [key for key in assignments.values() if key is not None]
        if len(assignments) != len(params["products"]) or assignments.keys() - products.keys() or len(targets) != len(set(targets)):
            raise AppError("invalid_replacement", "替换商品不得重复或来自其他批次", 422)
        for p in products.values():
            target = assignments.get(p.id)
            if target is not None and (target not in formal or formal[target]["kind"] != p.kind):
                raise AppError("invalid_replacement", "替换目标须为同项目、同类型的有效商品", 422)
            if target == p.existing_product_id:
                continue
            previous = p.existing_product_id
            original_tags = {row["field_key"]: row["value"] for row in above["tags"] if row["product_id"] == target}
            p.existing_product_id = target
            p.tags_confirmation = {key: {"status": "pending", "previous_value": original_tags.get(key, "") if target else None}
                                   for key in FIXED_FIELDS}
            if p.kind == "set":
                p.relation_confirmed, p.relation_confirm_kind = False, "pending"
                p.flags = list(dict.fromkeys([*p.flags, "relation_recheck"]))
            before = {"existing_product_id": previous}
            self.review._log(session, user, "明确选择主图替换对象", "batch_product", p.id, before,
                {"existing_product_id": target, "reset_tag_confirmations": True}, host)
        self._validate_relations(params, formal, products)

    @staticmethod
    def _validate_relations(params: dict, formal: dict, products: dict) -> None:
        formal_sets = {str(key) for key, row in formal.items() if row["kind"] == "set"}
        formal_items = {key for key, row in formal.items() if row["kind"] == "item"}
        sets = {str(key) for key, row in products.items() if row.kind == "set"}
        items = {key for key, row in products.items() if row.kind == "item"}
        for name, allowed_keys, allowed_values in (
                ("relation_reviews", formal_sets, formal_items), ("batch_set_items", sets, formal_items),
                ("relation_batch_items", formal_sets, items)):
            for key, values in params[name].items():
                if key not in allowed_keys or set(values) - allowed_values or len(set(values)) != len(values):
                    raise AppError("invalid_relation", "套装关系中存在无效或重复商品", 422)

    def supplement_snapshot(self, session: Session, batch: Batch, content: dict) -> dict:
        project = project_for_supplement(session, batch.project_id)
        params = self._parse("supplement", self._changes(batch))
        files = {row.id: row for row in session.scalars(select(BatchFile).where(
            BatchFile.batch_id == batch.id, BatchFile.removed.is_not(True)))}
        current_files = catalog_files(session, project.id)
        unresolved = sorted(row.id for row in files.values() if row.conflict == "conflict" and row.conflict_choice is None)
        if unresolved:
            raise AppError("conflict_choice_required", "请明确选择替换或另存冲突文件", 409, {"file_ids": unresolved})
        if not any(row.conflict != "duplicate" for row in files.values()):
            raise AppError("no_changes", "所有文件均与正式文件重复，没有需要发布的变更", 409)
        snapshot = self._freeze(session, project, params)
        formal = {row["id"]: row for row in snapshot["before"]["products"] if row["status"] == "active"}
        products = {row.id: row for row in active_products(session, batch.id)}
        unassigned = products.keys() - {entry["id"] for entry in params["products"]}
        if unassigned:
            raise AppError("assignment_required", "请选择新增商品，或明确指定替换的原商品", 409,
                           {"batch_product_ids": sorted(unassigned)})
        self._validate_relations(params, formal, products)
        incomplete = set(self._required_relations(session, batch, snapshot["before"])) - {
            int(key) for key in params["relation_reviews"]}
        if incomplete:
            raise AppError("relation_recheck_required", "请逐项复核受影响的原套装关系", 409,
                           {"product_ids": sorted(incomplete)})
        replacement_sets = [p.id for p in products.values() if p.kind == "set" and p.existing_product_id is not None
                            and str(p.id) not in params["batch_set_items"]]
        if replacement_sets:
            raise AppError("relation_recheck_required", "替换套装主图需要明确复核原有单品关系", 409,
                           {"batch_product_ids": replacement_sets})
        for product in products.values():
            file = files[product.batch_file_id]
            if file.conflict == "conflict" and file.conflict_choice == "replace":
                old_file = current_files[file.rel_path.casefold()]
                old_product_id = next((row["id"] for row in formal.values() if row["file_id"] == old_file.id), None)
                if product.existing_product_id is None or old_product_id != product.existing_product_id:
                    raise AppError("replacement_target_required", "请手动选择同名主图所属的原商品，或改为另存",
                                   409, {"batch_product_id": product.id})
        snapshot["params"]["files"] = [{"id": f.id, "choice": f.conflict_choice} for f in files.values() if f.conflict == "conflict"]
        content["operation"] = snapshot
        snapshot["after"] = self._difference(prevalidate(session, batch, content))
        return snapshot
