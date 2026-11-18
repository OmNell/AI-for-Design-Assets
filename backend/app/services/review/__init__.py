"""上传人核对：改标签、确认套装关系、追加/移除主图、提交审核。

NAS 文件操作一律使用当前操作人本人的会话；所有修改写字段级 oplog。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, ExitStack
from uuid import uuid4
import hashlib
import json
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, BatchProduct, Oplog, PreflightAck, ReviewSnapshot, TagField, User, utcnow
from app.errors import AppError
from app.schemas.review import (BulkRequest, FolderFile, FolderGroup, OwnerResponse, Pending, ProductPatch, ProductUpdate,
                                RelationView, ReviewBatch, ReviewField, ReviewProduct, ReviewResponse, TagValue, TagView)
from app.services.ai.orchestrator import RELATION_FLAGS, REVIEWING, AiService, active_products
from app.services.ai.field_catalog import fixed_fields, FIXED_FIELDS
from app.services.ai.vision import has_visual_evidence
from app.services.ai.parsing import split_terms
from app.services.auth.service import AuthService, LoginContext
from app.services.nas import NasAlreadyExists, NasUnavailable, NasPermissionDenied, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED, RESERVED
from app.services.thumbs import ThumbService, is_main_image
from app.services.upload.fingerprint import fingerprint
from app.services.upload.service import UploadService

SUBMITTED = "待初审"
REJECTED = "已驳回"
VALUE_MAX = 500
FREE_TEXT_MAX = 5000
NOTE_MAX = 2000
RECYCLE = "_回收区"
RECHECK = "relation_recheck"
KIND_DIRS = {"set": "套装", "item": "单品"}
UPLOAD_ROLES = {"uploader", "first_reviewer", "final_reviewer"}


def _tag(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _brief(tag: dict[str, object] | None) -> dict[str, object] | None:
    return dict(tag) if tag else None


@contextmanager
def undo_nas_on_error() -> Iterator[ExitStack]:
    """数据库提交失败时，撤销本请求已完成的临时区移动。"""
    with ExitStack() as rollback:
        yield rollback
        rollback.pop_all()


class ReviewService:
    def __init__(self, auth: AuthService, thumbs: ThumbService, ai: AiService) -> None:
        self.auth, self.thumbs, self.ai = auth, thumbs, ai
        self.paths = create_paths(auth.config)

    # ---- 权限与读取 ----
    @staticmethod
    def _user(session: Session, actor: User) -> User:
        user = session.get(User, actor.id)
        if user is None or not user.enabled:
            raise AppError("forbidden", "当前账号没有此操作权限", 403)
        return user

    def _readable(self, session: Session, batch_id: int, actor: User) -> tuple[Batch, User]:
        user = self._user(session, actor)
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        if not ("admin" in user.roles or user.id in (batch.owner_id, batch.uploader_id)
                or user.id == batch.first_reviewer_id and "first_reviewer" in user.roles
                or user.id == batch.final_reviewer_id and "final_reviewer" in user.roles):
            raise AppError("forbidden", "无权查看该批次", 403)
        return batch, user

    def _writable(self, session: Session, batch_id: int, actor: User) -> tuple[Batch, User]:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = self._user(session, actor)
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        if user.id != batch.owner_id or not set(user.roles) & UPLOAD_ROLES:
            raise AppError("forbidden", "只有当前批次责任人可以核对", 403)
        if batch.status not in REVIEWING:
            raise AppError("invalid_batch_state", "批次不在待核对或已驳回状态，不能修改", 409)
        return batch, user

    @staticmethod
    def _product(session: Session, batch: Batch, product_id: int) -> tuple[BatchProduct, BatchFile]:
        product = session.get(BatchProduct, product_id)
        file = session.get(BatchFile, product.batch_file_id) if product is not None else None
        if product is None or product.batch_id != batch.id or file is None or file.batch_id != batch.id or file.removed:
            raise AppError("product_not_found", "主图不存在或已移除", 404)
        return product, file

    @staticmethod
    def _fields(session: Session) -> list[TagField]:
        return fixed_fields(session)

    @staticmethod
    def _pending(session: Session, batch: Batch, products: list[BatchProduct]) -> Pending:
        files = {f.id: f for f in session.scalars(select(BatchFile).where(
            BatchFile.batch_id == batch.id, BatchFile.removed.is_not(True)))}
        mains = {f.id for f in files.values() if is_main_image(f) and f.size > 0}
        invalid = sum(is_main_image(f) and f.size <= 0 for f in files.values())
        for f in files.values():
            if (f.upload_status != "done" or f.bytes_received != f.size or f.fingerprint_kind != "full"
                    or not re.fullmatch(r"[0-9a-f]{64}", f.fingerprint or "")):
                invalid += 1
        invalid += len(mains.symmetric_difference(p.batch_file_id for p in products))
        fields = yellow = relations = failed = processing = 0
        items = {p.id for p in products if p.kind == "item"}
        for product in products:
            file = files[product.batch_file_id]
            if product.kind != ("set" if file.rel_path.startswith("套装/") else "item"):
                invalid += 1
            if product.ai_status == "pending":
                processing += 1
            elif product.ai_status != "done" or not has_visual_evidence(product, file):
                failed += 1
            for key in FIXED_FIELDS:
                tag = product.tags.get(key, {})
                confirmation = product.tags_confirmation.get(key, {})
                expected = "confirmed_value" if tag.get("value", "").strip() else "confirmed_unknown"
                if (confirmation.get("status") != expected or confirmation.get("fingerprint") != file.fingerprint
                        or confirmation.get("value") != tag.get("value") or not confirmation.get("by")):
                    fields += 1
                yellow += bool(tag.get("flag"))
            yellow += len(product.flags)
            if product.kind == "set":
                members = {r.get("batch_product_id") for r in product.relation_items}
                expected = "confirmed_members" if members else "confirmed_empty"
                if not product.relation_confirmed or product.relation_confirm_kind != expected or not members <= items:
                    relations += 1
        missing = batch.kind == "ingest" and not mains
        return Pending(yellow=yellow, relations_unconfirmed=relations, failed=failed, processing=processing,
                       fields_unconfirmed=fields, invalid_files=invalid, missing_main_image=missing,
                       can_submit=not any((yellow, relations, failed, processing, fields, invalid, missing)))

    def _view(self, session: Session, product: BatchProduct, keys: set[str]) -> ReviewProduct:
        file = session.get(BatchFile, product.batch_file_id)
        tags = {key: TagView(confirmation_status=(product.tags_confirmation.get(key, {}).get("status", "pending")
                             if product.tags_confirmation.get(key, {}).get("fingerprint") == file.fingerprint else "pending"),
                             previous_value=product.tags_confirmation.get(key, {}).get("previous_value"), value=str(tag.get("value", "")), terms=[str(term) for term in tag.get("terms", []) or []],
                             source=str(tag.get("source", "ai")), ai_value=str(tag.get("ai_value", "")),
                             flag=tag.get("flag") if isinstance(tag.get("flag"), str) else None)
                for key in keys if (tag := _tag(product.tags.get(key, {}))) is not None}
        return ReviewProduct(id=product.id, batch_file_id=file.id, kind=product.kind, display_name=product.display_name,
            file_name=file.rel_path.rsplit("/", 1)[-1], fingerprint=file.fingerprint, thumb_status=file.thumb_status,
            thumb_error=file.thumb_error, ai_status=product.ai_status, ai_error=product.ai_error,
            visual_ai_succeeded=has_visual_evidence(product, file), tags=tags, free_text=product.free_text,
            free_text_source=product.free_text_source, ai_free_text=product.ai_free_text, flags=list(product.flags),
            relation_items=[RelationView(batch_product_id=item["batch_product_id"], source=str(item.get("source", "ai")))
                            for item in product.relation_items if isinstance(item, dict) and isinstance(item.get("batch_product_id"), int)],
            relation_confirmed=product.relation_confirmed, relation_confirm_kind=product.relation_confirm_kind)

    def _update(self, session: Session, batch: Batch, product: BatchProduct) -> ProductUpdate:
        session.flush()
        keys = {field.key for field in self._fields(session)}
        return ProductUpdate(product=self._view(session, product, keys),
                             pending=self._pending(session, batch, active_products(session, batch.id)))

    def _response(self, session: Session, batch: Batch, user: User) -> ReviewResponse:
        session.flush()
        fields = self._fields(session)
        keys = {field.key for field in fields}
        products = active_products(session, batch.id)
        groups: dict[str, list[FolderFile]] = {name: [] for name in REQUIRED}
        for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.rel_path)):
            if file.removed or is_main_image(file):
                continue
            head, _, rest = file.rel_path.partition("/")
            groups.setdefault(head if rest else "项目根目录", []).append(FolderFile(name=rest or head, size=file.size))
        owner = session.get(User, batch.owner_id)
        parts = str(batch.payload["folder_name"]).split("_")
        return ReviewResponse(
            batch=ReviewBatch(id=batch.id, batch_no=batch.batch_no, project_code=batch.project_code,
                folder_name=str(batch.payload["folder_name"]), company_name=parts[1] if len(parts) == 3 else "",
                project_name=parts[2] if len(parts) == 3 else "", status=batch.status, won=batch.won, round=batch.round,
                owner_id=batch.owner_id, owner_name=(owner.display_name or owner.username) if owner else "",
                uploader_id=batch.uploader_id, recommended_path=batch.recommended_path, note_to_reviewer=batch.note_to_reviewer,
                review_snapshot_hash=batch.review_snapshot_hash,
                editable=batch.status in REVIEWING and user.id == batch.owner_id and bool(set(user.roles) & UPLOAD_ROLES)),
            fields=[ReviewField(key=field.key, label=field.label, hint=field.hint) for field in fields],
            products=[self._view(session, product, keys) for product in products],
            pending=self._pending(session, batch, products),
            folders=[FolderGroup(name=name, files=files) for name, files in groups.items()])

    def read(self, batch_id: int, actor: User) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._readable(session, batch_id, actor)
            if batch.review_snapshot_hash and batch.status not in REVIEWING:
                frozen = session.scalar(select(ReviewSnapshot).where(ReviewSnapshot.batch_id == batch.id,
                                                                      ReviewSnapshot.round == batch.round))
                if frozen is None or frozen.digest != batch.review_snapshot_hash:
                    raise AppError("snapshot_missing", "找不到本轮提交版本", 409)
                view = ReviewResponse.model_validate(frozen.content["review"])
                view.batch.status, view.batch.review_snapshot_hash = batch.status, frozen.digest
                return view
            return self._response(session, batch, user)

    # ---- 标签 ----
    @staticmethod
    def _set_tags(product: BatchProduct, changes: dict[str, str], keys: set[str],
                  before: dict[str, object], after: dict[str, object]) -> None:
        tags = dict(product.tags)
        for key, raw in changes.items():
            if key not in keys:
                raise AppError("unknown_field", "标签字段不存在或已停用", 422, {"field": key})
            value = raw.strip()
            if len(value) > VALUE_MAX:
                raise AppError("value_too_long", f"每个字段最多 {VALUE_MAX} 字", 422, {"field": key})
            old = _tag(tags.get(key))
            if (old is None and not value) or (old is not None and str(old.get("value", "")).strip() == value):
                continue
            # 人工修改：保留 AI 原值，重新拆分 terms，并视为已处理标黄。
            new = {"value": value, "terms": split_terms(value), "source": "manual",
                   "ai_value": old.get("ai_value", "") if old else "", "flag": None}
            previous = product.tags_confirmation.get(key, {})
            before[key] = {"tag": _brief(old), "confirmation": previous}
            product.tags_confirmation = {**product.tags_confirmation, key: {
                "status": "pending", "previous_value": previous.get("previous_value")}}
            after[key] = {"tag": _brief(new), "confirmation": product.tags_confirmation[key]}
            tags[key] = new
        product.tags = tags

    def _log(self, session: Session, user: User, action: str, kind: str, object_id: int,
             before: object, after: object, host: str) -> None:
        OplogService(session).write(user.id, ",".join(user.roles), action, kind, str(object_id), before, after, "成功", host)

    def patch(self, batch_id: int, product_id: int, body: ProductPatch, actor: User, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, file = self._product(session, batch, product_id)
            keys = {field.key for field in self._fields(session)}
            before: dict[str, object] = {}
            after: dict[str, object] = {}
            self._set_tags(product, body.tags, keys, before, after)
            tags = dict(product.tags)
            if body.confirmations and (product.ai_status != "done" or not has_visual_evidence(product, file)):
                raise AppError("visual_evidence_required", "请先完成当前主图的视觉识别，再确认标签", 409)
            for key, status in body.confirmations.items():
                if key not in keys:
                    raise AppError("unknown_field", "字段不在固定标签清单内", 422)
                tag = tags.get(key, {"ai_value": "", "value": "", "terms": [], "source": "ai", "flag": None})
                value = tag["value"].strip() if status == "confirmed_value" else ""
                if status == "confirmed_value" and (not value or value in ("无法判断", "无法确定")):
                    raise AppError("empty_confirmation", "请填写具体值，或选择确认无法判断", 422, {"field": key})
                old = product.tags_confirmation.get(key, {})
                source = "manual" if tag["source"] == "manual" or tag["value"] != value else "ai_confirmed"
                new = {**tag, "value": value, "terms": split_terms(value), "source": source, "flag": None}
                record = {"status": status, "by": user.id, "at": utcnow().isoformat(), "value": value,
                          "fingerprint": file.fingerprint, "previous_value": old.get("previous_value")}
                before.setdefault(key, {"tag": tag, "confirmation": old})
                after[key] = {"tag": new, "confirmation": record}
                tags[key] = new
                product.tags_confirmation = {**product.tags_confirmation, key: record}
            product.tags = tags
            before_log: dict[str, object] = {"tags": before} if before else {}
            after_log: dict[str, object] = {"tags": after} if after else {}
            if body.free_text is not None:
                text = body.free_text.strip()
                if len(text) > FREE_TEXT_MAX:
                    raise AppError("value_too_long", f"自由描述最多 {FREE_TEXT_MAX} 字", 422, {"field": "free_text"})
                if text != product.free_text:
                    before_log["free_text"], after_log["free_text"] = product.free_text, text
                    product.free_text, product.free_text_source = text, "manual"
            resolved = [flag for flag in body.resolve_flags if flag in product.flags]
            if resolved:
                before_log["flags"] = list(product.flags)
                product.flags = [flag for flag in product.flags if flag not in resolved]
                after_log["flags"] = list(product.flags)
            if after_log:
                self._log(session, user, "核对修改商品", "batch_product", product.id, before_log, after_log, host)
            return self._update(session, batch, product)

    def bulk(self, batch_id: int, body: BulkRequest, actor: User, host: str) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            keys = {field.key for field in self._fields(session)}
            for product_id in body.product_ids:
                product, _ = self._product(session, batch, product_id)
                before: dict[str, object] = {}
                after: dict[str, object] = {}
                self._set_tags(product, body.tags, keys, before, after)
                if after:
                    self._log(session, user, "批量修改商品标签", "batch_product", product.id, {"tags": before}, {"tags": after}, host)
            return self._response(session, batch, user)

    def tag_values(self, field: str, q: str, batch_id: int | None, actor: User) -> list[TagValue]:
        with self.auth.lock, self.auth.database.session() as session:
            self._user(session, actor)
            if field not in FIXED_FIELDS:
                raise AppError("unknown_field", "字段不在固定标签清单内", 422)
            # WP09 建立正式商品库之前，仅从当前有权读取的批次提供输入参考。
            if batch_id is None:
                return []
            self._readable(session, batch_id, actor)
            needle = q.strip().casefold()
            counter: Counter[str] = Counter()
            for product in active_products(session, batch_id):
                tag = _tag(product.tags.get(field))
                for term in (tag.get("terms") or []) if tag else []:
                    if isinstance(term, str) and term and needle in term.casefold():
                        counter[term] += 1
            ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:20]
            return [TagValue(term=term, count=count) for term, count in ranked]

    # ---- 单张重试、套装关系 ----
    def retry_ai(self, batch_id: int, product_id: int, login: LoginContext, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, login.user)
            product, file = self._product(session, batch, product_id)
            if product.ai_status == "pending":
                raise AppError("ai_in_progress", "这张图正在打标，请稍候", 409)
            if file.fingerprint_kind != "full" or not file.fingerprint:
                path = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch), file.rel_path)
                fs = self.auth.pool.get(login.session_id)
                before_digest = {"fingerprint": file.fingerprint, "kind": file.fingerprint_kind}
                UploadService._record_fingerprint(batch, file, fingerprint(fs, path, file.size))
                self._log(session, user, "补算核对主图完整指纹", "batch_file", file.id, before_digest,
                          {"fingerprint": file.fingerprint, "kind": "full"}, host)
            before = {"ai_status": product.ai_status, "ai_error": product.ai_error}
            self.ai.start_single(session, batch, product, user, host, login.session_id)
            self._log(session, user, "单张重新打标", "batch_product", product.id, before, {"ai_status": product.ai_status}, host)
            return self._update(session, batch, product)

    def relations(self, batch_id: int, product_id: int, item_ids: list[int], confirm_kind: str, actor: User, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, file = self._product(session, batch, product_id)
            if product.kind != "set":
                raise AppError("not_a_set", "只有套装图可以设置包含的单品", 422)
            if product.ai_status != "done" or not has_visual_evidence(product, file):
                raise AppError("visual_evidence_required", "请先完成当前套装的视觉识别，再确认关系", 409)
            if confirm_kind != ("confirmed_members" if item_ids else "confirmed_empty"):
                raise AppError("relation_confirmation_required", "请明确确认所选单品或无关联单品", 422)
            valid = {item.id for item in active_products(session, batch.id, "item")}
            unknown = [item_id for item_id in item_ids if item_id not in valid]
            if unknown:
                raise AppError("unknown_item", "所选单品不在本批次中或已移除", 422, {"item_ids": unknown})
            sources = {item["batch_product_id"]: item.get("source", "ai") for item in product.relation_items if isinstance(item, dict)}
            before = {"relation_items": list(product.relation_items), "relation_confirmed": product.relation_confirmed, "flags": list(product.flags)}
            # AI 推荐并保留的仍记 ai；人工补勾的记 manual。
            product.relation_items = [{"batch_product_id": item_id, "source": sources.get(item_id, "manual")}
                                      for item_id in dict.fromkeys(item_ids)]
            product.relation_confirmed = True
            product.relation_confirm_kind = confirm_kind
            product.flags = [flag for flag in product.flags if flag not in RELATION_FLAGS]
            self._log(session, user, "确认套装关系", "batch_product", product.id, before,
                      {"relation_items": product.relation_items, "relation_confirmed": True, "relation_confirm_kind": confirm_kind, "flags": product.flags}, host)
            return self._update(session, batch, product)

    # ---- 追加与移除主图 ----
    def _folder(self, batch: Batch) -> str:
        return str(batch.payload["folder_name"])

    def append_limit(self) -> int:
        return self.auth.config.upload.append_max_mb * 1024 * 1024

    def append(self, batch_id: int, kind: str, filename: str, data: bytes, mtime: int | None, login: LoginContext, host: str) -> ReviewResponse:
        with self.auth.lock, undo_nas_on_error() as rollback, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, login.user)
            if kind not in KIND_DIRS:
                raise AppError("invalid_kind", "请选择放入套装或单品", 422)
            name = filename.strip()
            try:
                if "/" in name or "\\" in name or safe_join(name) != name:
                    raise NasPermissionDenied("文件名非法")
            except NasPermissionDenied:
                raise AppError("invalid_filename", "文件名含非法字符", 422) from None
            if "." not in name or name.rsplit(".", 1)[1].lower() not in ("jpg", "jpeg", "png") or not name.rsplit(".", 1)[0]:
                raise AppError("invalid_filename", "主图只能是 jpg、jpeg、png 图片", 422)
            if name.casefold() in {value.casefold() for value in RESERVED}:
                raise AppError("invalid_filename", "文件名是系统保留名称", 422)
            if not data or len(data) > self.append_limit():
                raise AppError("invalid_file_size", f"图片不能为空，且不超过 {self.auth.config.upload.append_max_mb}MB", 413 if data else 422)
            rel_path = f"{KIND_DIRS[kind]}/{name}"
            nas = self.auth.config.nas
            prefix = len(f"\\\\{nas.host}\\{nas.share}\\{self.paths.root(Region.TEMP)}\\B00000000-000000\\")
            if prefix + len(self._folder(batch) + "/" + rel_path) > 200:
                raise AppError("path_too_long", "项目内完整路径超过 200 字符", 422)
            reuse: BatchFile | None = None
            for other in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)):
                if other.rel_path == rel_path and other.removed:
                    reuse = other
                elif other.rel_path.casefold() == rel_path.casefold() and not other.removed:
                    raise AppError("file_exists", "同名主图已存在，请改名后再追加", 409)
            # 用当前操作人本人的 NAS 会话写入临时区。
            fs = self.auth.pool.get(login.session_id)
            path = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch), rel_path)
            if fs.exists(path):
                raise AppError("file_exists", "临时区已存在同名文件，请改名后再追加", 409)
            parent = path.rsplit("/", 1)[0]
            if not fs.exists(parent):
                fs.mkdir(parent, recursive=True)
            stage_dir = self.paths.join(Region.TEMP, batch.batch_no, RECYCLE, "追加暂存")
            if not fs.exists(stage_dir):
                fs.mkdir(stage_dir, recursive=True)
            stage = safe_join(stage_dir, uuid4().hex)
            rollback.callback(lambda: fs.delete(stage) if fs.exists(stage) else None)
            with fs.open_write(stage) as stream:
                if stream.write(data) != len(data):
                    raise NasUnavailable("主图未完整写入，请重新追加")
            try:
                fs.rename(stage, path)
            except NasAlreadyExists:
                raise AppError("file_exists", "临时区已存在同名主图", 409) from None
            rollback.callback(fs.rename, path, stage)
            digest = hashlib.sha256(data).hexdigest()
            stamp = mtime if mtime is not None else int(time.time() * 1000)
            file = reuse or BatchFile(batch_id=batch.id, rel_path=rel_path, size=0, mtime=0)
            file.size, file.mtime, file.bytes_received, file.fingerprint = len(data), stamp, len(data), digest
            file.upload_status, file.removed, file.thumb_status, file.thumb_error = "done", False, None, None
            file.fingerprint_kind, file.thumb_fingerprint = "full", None
            session.add(file)
            session.flush()
            batch.payload = {**batch.payload, "fingerprint_kinds": {**batch.payload.get("fingerprint_kinds", {}), str(file.id): "full"}}
            product = session.scalar(select(BatchProduct).where(BatchProduct.batch_id == batch.id, BatchProduct.batch_file_id == file.id))
            if product is None:
                product = BatchProduct(batch_id=batch.id, batch_file_id=file.id, kind=kind, display_name=name.rsplit(".", 1)[0])
                session.add(product)
            product.kind, product.display_name = kind, name.rsplit(".", 1)[0]
            product.tags_confirmation = {k: {"previous_value": v.get("value", ""), "status": "pending"}
                                         for k, v in (product.tags or {}).items() if k in FIXED_FIELDS}
            product.ai_model = product.ai_fingerprint = product.ai_success_at = None
            product.relation_confirm_kind = "pending"
            product.tags, product.free_text, product.free_text_source, product.ai_free_text = {}, "", "ai", ""
            product.flags, product.relation_items, product.relation_confirmed = [], [], False
            session.flush()
            rechecked: list[int] = []
            if kind == "item":
                # 已确认的关系不自动改变，但要求责任人重新复核。
                for other in active_products(session, batch.id, "set"):
                    if other.id == product.id:
                        continue
                    other.relation_confirm_kind, other.relation_confirmed = "pending", False
                    if RECHECK not in other.flags:
                        other.flags = [*other.flags, RECHECK]
                    rechecked.append(other.id)
            self.thumbs.enqueue(session, batch, file, actor_id=user.id, session_id=login.session_id, retry=True)
            self.ai.start_single(session, batch, product, user, host, login.session_id)
            self._log(session, user, "追加主图", "batch_product", product.id, None,
                      {"rel_path": rel_path, "size": len(data), "fingerprint": digest, "sets_to_recheck": rechecked}, host)
            return self._response(session, batch, user)

    def remove(self, batch_id: int, product_id: int, login: LoginContext, host: str) -> ReviewResponse:
        with self.auth.lock, undo_nas_on_error() as rollback, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, login.user)
            product, file = self._product(session, batch, product_id)
            fs = self.auth.pool.get(login.session_id)
            source = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch), file.rel_path)
            directory, name = file.rel_path.split("/", 1)
            if not fs.exists(source):
                raise AppError("source_missing", "临时主图不存在，请先核对回收区与文件清单", 409)

            target_dir = self.paths.join(Region.TEMP, batch.batch_no, RECYCLE, directory)
            if not fs.exists(target_dir):
                fs.mkdir(target_dir, recursive=True)
            stem, extension = name.rsplit(".", 1)
            candidate, index = name, 1
            while fs.exists(target_dir + "/" + candidate):
                index += 1
                candidate = f"{stem}_{index}.{extension}"
            fs.rename(source, target_dir + "/" + candidate)
            rollback.callback(fs.rename, target_dir + "/" + candidate, source)
            recycled = f"{RECYCLE}/{directory}/{candidate}"
            file.removed = True
            self.ai.cancel_single(session, batch, product, "主图已移除", user.id, host)
            affected: list[int] = []
            if product.kind == "item":
                for other in active_products(session, batch.id, "set"):
                    kept = [item for item in other.relation_items if not (isinstance(item, dict) and item.get("batch_product_id") == product.id)]
                    if len(kept) != len(other.relation_items):
                        other.relation_items = kept
                        other.relation_confirm_kind, other.relation_confirmed = "pending", False
                        if RECHECK not in other.flags:
                            other.flags = [*other.flags, RECHECK]
                        affected.append(other.id)
            self._log(session, user, "移除主图", "batch_product", product.id,
                      {"rel_path": file.rel_path, "removed": False, "in_sets": affected},
                      {"removed": True, "recycle_path": recycled, "in_sets": []}, host)
            return self._response(session, batch, user)

    # ---- 提交与责任人 ----
    @staticmethod
    def _digest(content: dict[str, object]) -> str:
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _submission(self, session: Session, batch: Batch, user: User, note: str) -> dict[str, object]:
        view = self._response(session, batch, user)
        if not view.pending.can_submit:
            raise AppError("submit_blocked", "还有未处理的项，暂不能提交审核", 409, view.pending.model_dump())
        files = list(session.scalars(select(BatchFile).where(
            BatchFile.batch_id == batch.id, BatchFile.removed.is_not(True)).order_by(BatchFile.id)))
        products = active_products(session, batch.id)
        evidence = {str(p.id): {"ai_model": p.ai_model, "ai_success_at": p.ai_success_at.isoformat(),
                    "ai_fingerprint": p.ai_fingerprint, "tags_confirmation": p.tags_confirmation,
                    "existing_product_id": p.existing_product_id} for p in products}
        actions = list(session.scalars(select(Oplog.id).where(Oplog.object_type == "batch_product",
            Oplog.object_id.in_([str(p.id) for p in products])).order_by(Oplog.id)))
        view.batch.round += int(batch.status == REJECTED)
        view.batch.status, view.batch.editable = SUBMITTED, False
        view.batch.note_to_reviewer, view.batch.review_snapshot_hash = note or None, None
        view.pending.can_submit = False
        return {"schema": 1, "submitted_by": user.id, "kind": batch.kind, "project_id": batch.project_id,
                "review": view.model_dump(mode="json"), "evidence": evidence, "confirmation_oplog_ids": actions,
                "directories": batch.payload.get("directories", []),
                "preflight_acks": list(session.scalars(select(PreflightAck.rule_code).where(
                    PreflightAck.batch_id == batch.id).order_by(PreflightAck.rule_code))),
                "files": [{"id": f.id, "rel_path": f.rel_path, "size": f.size, "mtime": f.mtime,
                           "fingerprint": f.fingerprint, "fingerprint_kind": f.fingerprint_kind,
                           "conflict": f.conflict, "conflict_choice": f.conflict_choice} for f in files]}

    def submit(self, batch_id: int, note: str, login: LoginContext, host: str) -> ReviewResponse:
        note = note.strip()
        if len(note) > NOTE_MAX:
            raise AppError("value_too_long", f"备注最多 {NOTE_MAX} 字", 422)
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, login.user)
            content = self._submission(session, batch, user, note)
            digest = self._digest(content)
            root = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch))
        # 大文件重读放在写事务外，期间其他设备可以工作。入库执行时还需再次校验冻结指纹。
        fs = self.auth.pool.get(login.session_id)
        for file in content["files"]:
            path = safe_join(root, file["rel_path"])
            if (not fs.exists(path) or fs.stat(path).size != file["size"]
                    or fingerprint(fs, path, file["size"]) != file["fingerprint"]):
                raise AppError("review_file_changed", "临时文件与已上传的完整指纹不一致", 409, {"file_id": file["id"]})
        # 枚举待审目录，避免把清单外文件随整目录一起交给后续入库。
        children: dict[str, set[str]] = defaultdict(set)
        directories = {"", *content["directories"]}
        for relative in [*content["directories"], *(file["rel_path"] for file in content["files"])]:
            parts = relative.split("/")
            for index, name in enumerate(parts):
                parent = "/".join(parts[:index])
                directories.add(parent)
                children[parent].add(name)
        for relative in sorted(directories):
            path = safe_join(root, relative) if relative else root
            if not fs.exists(path) or not fs.stat(path).is_dir:
                raise AppError("review_file_changed", "上传清单中的目录缺失", 409, {"directory": relative})
            actual = set(fs.list(path))
            if relative == "":
                # 核对时移除最后一张图会保留其空目录。
                for name in (actual - children[relative]) & {"套装", "单品"}:
                    folder = safe_join(root, name)
                    if fs.stat(folder).is_dir and not fs.list(folder):
                        actual.remove(name)
            if actual != children[relative]:
                raise AppError("review_file_changed", "临时区目录内容与提交清单不一致", 409,
                               {"directory": relative, "extra": sorted(actual - children[relative]),
                                "missing": sorted(children[relative] - actual)})
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, login.user)
            UploadService._check_login(session, login)
            self.auth.pool.get(login.session_id)
            if self._digest(self._submission(session, batch, user, note)) != digest:
                raise AppError("review_changed", "核对内容已发生变化，请刷新后重新提交", 409)
            before = {"status": batch.status, "round": batch.round}
            view = ReviewResponse.model_validate(content["review"])
            batch.round, batch.note_to_reviewer = view.batch.round, view.batch.note_to_reviewer
            batch.status, batch.review_snapshot_hash = SUBMITTED, digest
            batch.first_reviewer_id = batch.final_reviewer_id = None
            session.add(ReviewSnapshot(batch_id=batch.id, round=batch.round, submitted_by=user.id, digest=digest, content=content))
            self._log(session, user, "提交审核", "batch", batch.id, before,
                      {"status": SUBMITTED, "round": batch.round, "review_snapshot_hash": digest}, host)
            view.batch.review_snapshot_hash = digest
            return view

    def change_owner(self, batch_id: int, user_id: int, actor: User, host: str) -> OwnerResponse:
        with self.auth.lock, self.auth.database.session() as session:
            admin = self._user(session, actor)
            if "admin" not in admin.roles:
                raise AppError("forbidden", "只有管理员可以更换责任人", 403)
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            if batch.status not in REVIEWING:
                raise AppError("invalid_batch_state", "当前状态不能更换责任人", 409)
            target = session.get(User, user_id)
            if target is None:
                raise AppError("user_not_found", "用户不存在，请让该用户先登录一次", 404)
            if not target.enabled or not set(target.roles) & UPLOAD_ROLES:
                raise AppError("invalid_owner", "新责任人必须是启用且具有上传权限的账号", 422)
            if batch.payload.get("ai_single"):
                raise AppError("ai_in_progress", "请等待本次主图打标结束后更换责任人", 409)
            before = {"owner_id": batch.owner_id}
            batch.owner_id = target.id
            self._log(session, admin, "更换批次责任人", "batch", batch.id, before, {"owner_id": target.id}, host)
            return OwnerResponse(id=batch.id, owner_id=target.id, owner_name=target.display_name or target.username)
