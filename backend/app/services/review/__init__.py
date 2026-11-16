"""上传人核对：改标签、确认套装关系、追加/移除主图、提交审核。

NAS 文件操作一律使用当前操作人本人的会话；所有修改写字段级 oplog。
"""
from __future__ import annotations

from collections import Counter
import hashlib
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, BatchProduct, TagField, User
from app.errors import AppError
from app.schemas.review import (BulkRequest, FolderFile, FolderGroup, OwnerResponse, Pending, ProductPatch, ProductUpdate,
                                RelationView, ReviewBatch, ReviewField, ReviewProduct, ReviewResponse, TagValue, TagView)
from app.services.ai.orchestrator import RELATION_FLAGS, REVIEWING, AiService, active_products
from app.services.ai.parsing import split_terms
from app.services.auth.service import AuthService
from app.services.nas import NasPermissionDenied, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED, RESERVED
from app.services.thumbs import ThumbService, is_main_image
from app.services.upload.fingerprint import fingerprint

SUBMITTED = "待初审"
REJECTED = "已驳回"
INGESTED = "已入库"
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
    return None if tag is None else {"value": tag.get("value", ""), "source": tag.get("source"), "flag": tag.get("flag")}


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
        user = self._user(session, actor)
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        if "admin" not in user.roles and user.id != batch.owner_id:
            raise AppError("forbidden", "只有批次责任人和管理员可以核对", 403)
        if batch.status not in REVIEWING:
            raise AppError("invalid_batch_state", "批次不在待核对或已驳回状态，不能修改", 409)
        return batch, user

    @staticmethod
    def _product(session: Session, batch: Batch, product_id: int) -> tuple[BatchProduct, BatchFile]:
        product = session.get(BatchProduct, product_id)
        file = session.get(BatchFile, product.batch_file_id) if product is not None else None
        if product is None or product.batch_id != batch.id or file is None or file.removed:
            raise AppError("product_not_found", "主图不存在或已移除", 404)
        return product, file

    @staticmethod
    def _fields(session: Session) -> list[TagField]:
        return list(session.scalars(select(TagField).where(TagField.enabled.is_(True)).order_by(TagField.sort_order, TagField.id)))

    @staticmethod
    def _filled(product: BatchProduct, keys: set[str]) -> bool:
        return any(key in keys and tag.get("source") == "manual" and str(tag.get("value", "")).strip()
                   for key, value in product.tags.items() if (tag := _tag(value)) is not None)

    def _pending(self, products: list[BatchProduct], keys: set[str]) -> Pending:
        yellow = sum(1 for product in products for key, value in product.tags.items()
                     if key in keys and (tag := _tag(value)) is not None and tag.get("flag"))
        yellow += sum("multi_object" in product.flags for product in products)
        relations = sum(product.kind == "set" and not product.relation_confirmed for product in products)
        failed = sum(product.ai_status == "failed" and not self._filled(product, keys) for product in products)
        processing = sum(product.ai_status == "pending" for product in products)
        return Pending(yellow=yellow, relations_unconfirmed=relations, failed=failed, processing=processing,
                       can_submit=not (yellow or relations or failed or processing))

    def _view(self, session: Session, product: BatchProduct, keys: set[str]) -> ReviewProduct:
        file = session.get(BatchFile, product.batch_file_id)
        tags = {key: TagView(value=str(tag.get("value", "")), terms=[str(term) for term in tag.get("terms", []) or []],
                             source=str(tag.get("source", "ai")), ai_value=str(tag.get("ai_value", "")),
                             flag=tag.get("flag") if isinstance(tag.get("flag"), str) else None)
                for key, value in product.tags.items() if (tag := _tag(value)) is not None}
        return ReviewProduct(id=product.id, batch_file_id=file.id, kind=product.kind, display_name=product.display_name,
            file_name=file.rel_path.rsplit("/", 1)[-1], fingerprint=file.fingerprint, thumb_status=file.thumb_status,
            thumb_error=file.thumb_error, ai_status=product.ai_status, ai_error=product.ai_error,
            manually_filled=self._filled(product, keys), tags=tags, free_text=product.free_text,
            free_text_source=product.free_text_source, ai_free_text=product.ai_free_text, flags=list(product.flags),
            relation_items=[RelationView(batch_product_id=item["batch_product_id"], source=str(item.get("source", "ai")))
                            for item in product.relation_items if isinstance(item, dict) and isinstance(item.get("batch_product_id"), int)],
            relation_confirmed=product.relation_confirmed)

    def _update(self, session: Session, batch: Batch, product: BatchProduct) -> ProductUpdate:
        session.flush()
        keys = {field.key for field in self._fields(session)}
        return ProductUpdate(product=self._view(session, product, keys),
                             pending=self._pending(active_products(session, batch.id), keys))

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
                editable=batch.status in REVIEWING and ("admin" in user.roles or user.id == batch.owner_id)),
            fields=[ReviewField(key=field.key, label=field.label, hint=field.hint) for field in fields],
            products=[self._view(session, product, keys) for product in products],
            pending=self._pending(products, keys),
            folders=[FolderGroup(name=name, files=files) for name, files in groups.items()])

    def read(self, batch_id: int, actor: User) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._readable(session, batch_id, actor)
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
            before[key], after[key] = _brief(old), _brief(new)
            tags[key] = new
        product.tags = tags

    def _log(self, session: Session, user: User, action: str, kind: str, object_id: int,
             before: object, after: object, host: str) -> None:
        OplogService(session).write(user.id, ",".join(user.roles), action, kind, str(object_id), before, after, "成功", host)

    def patch(self, batch_id: int, product_id: int, body: ProductPatch, actor: User, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, _ = self._product(session, batch, product_id)
            keys = {field.key for field in self._fields(session)}
            before: dict[str, object] = {}
            after: dict[str, object] = {}
            self._set_tags(product, body.tags, keys, before, after)
            tags = dict(product.tags)
            for key in body.confirm_fields:
                tag = _tag(tags.get(key))
                if key not in keys or tag is None:
                    raise AppError("unknown_field", "该字段没有可确认的 AI 值", 422, {"field": key})
                if tag.get("source") == "manual":
                    raise AppError("field_already_edited", "该字段已人工修改，无需确认 AI 值", 409, {"field": key})
                if tag.get("source") == "ai_confirmed" and not tag.get("flag"):
                    continue
                new = {**tag, "source": "ai_confirmed", "flag": None}
                before[key], after[key] = _brief(tag), _brief(new)
                tags[key] = new
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
            # 卡片柜的 product_tags 在入库工作包落地；此前以已入库批次的商品标签作为卡片柜来源。
            batch_ids = set(session.scalars(select(Batch.id).where(Batch.status == INGESTED)))
            if batch_id is not None:
                self._readable(session, batch_id, actor)
                batch_ids.add(batch_id)
            needle = q.strip().casefold()
            counter: Counter[str] = Counter()
            for current in sorted(batch_ids):
                for product in active_products(session, current):
                    tag = _tag(product.tags.get(field))
                    for term in (tag.get("terms") or []) if tag else []:
                        if isinstance(term, str) and term and needle in term.casefold():
                            counter[term] += 1
            ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:20]
            return [TagValue(term=term, count=count) for term, count in ranked]

    # ---- 单张重试、套装关系 ----
    def retry_ai(self, batch_id: int, product_id: int, actor: User, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, _ = self._product(session, batch, product_id)
            if product.ai_status == "pending":
                raise AppError("ai_in_progress", "这张图正在打标，请稍候", 409)
            before = {"ai_status": product.ai_status, "ai_error": product.ai_error}
            self.ai.start_single(session, batch, product, user, host)
            self._log(session, user, "单张重新打标", "batch_product", product.id, before, {"ai_status": product.ai_status}, host)
            return self._update(session, batch, product)

    def relations(self, batch_id: int, product_id: int, item_ids: list[int], actor: User, host: str) -> ProductUpdate:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, _ = self._product(session, batch, product_id)
            if product.kind != "set":
                raise AppError("not_a_set", "只有套装图可以设置包含的单品", 422)
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
            product.flags = [flag for flag in product.flags if flag not in RELATION_FLAGS]
            self._log(session, user, "确认套装关系", "batch_product", product.id, before,
                      {"relation_items": product.relation_items, "relation_confirmed": True, "flags": product.flags}, host)
            return self._update(session, batch, product)

    # ---- 追加与移除主图 ----
    def _folder(self, batch: Batch) -> str:
        return str(batch.payload["folder_name"])

    def append_limit(self) -> int:
        return self.auth.config.upload.append_max_mb * 1024 * 1024

    def append(self, batch_id: int, kind: str, filename: str, data: bytes, mtime: int | None, actor: User, host: str) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
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
            fs = self.auth.pool.get(user.id)
            path = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch), rel_path)
            if fs.exists(path):
                raise AppError("file_exists", "临时区已存在同名文件，请改名后再追加", 409)
            parent = path.rsplit("/", 1)[0]
            if not fs.exists(parent):
                fs.mkdir(parent, recursive=True)
            try:
                with fs.open_write(path) as stream:
                    stream.write(data)
            except Exception:
                if fs.exists(path):
                    fs.delete(path)
                raise
            digest, digest_kind = fingerprint(fs, path, len(data), self.auth.config.upload.full_fingerprint_max_mb * 1024 * 1024,
                                              hashlib.sha256(data))
            stamp = mtime if mtime is not None else int(time.time() * 1000)
            file = reuse or BatchFile(batch_id=batch.id, rel_path=rel_path, size=0, mtime=0)
            file.size, file.mtime, file.bytes_received, file.fingerprint = len(data), stamp, len(data), digest
            file.upload_status, file.removed, file.thumb_status, file.thumb_error = "done", False, None, None
            session.add(file)
            session.flush()
            self.thumbs.invalidate(batch.id, file.id)
            batch.payload = {**batch.payload, "fingerprint_kinds": {**batch.payload.get("fingerprint_kinds", {}), str(file.id): digest_kind}}
            product = session.scalar(select(BatchProduct).where(BatchProduct.batch_id == batch.id, BatchProduct.batch_file_id == file.id))
            if product is None:
                product = BatchProduct(batch_id=batch.id, batch_file_id=file.id, kind=kind, display_name=name.rsplit(".", 1)[0])
                session.add(product)
            product.kind, product.display_name = kind, name.rsplit(".", 1)[0]
            product.tags, product.free_text, product.free_text_source, product.ai_free_text = {}, "", "ai", ""
            product.flags, product.relation_items, product.relation_confirmed = [], [], False
            session.flush()
            rechecked: list[int] = []
            if kind == "item":
                # 已确认的关系不自动改变，但要求责任人重新复核。
                for other in active_products(session, batch.id, "set"):
                    if other.id == product.id:
                        continue
                    other.relation_confirmed = False
                    if RECHECK not in other.flags:
                        other.flags = [*other.flags, RECHECK]
                    rechecked.append(other.id)
            self.thumbs.enqueue(session, batch, file, run_as=user.id)
            self.ai.start_single(session, batch, product, user, host)
            self._log(session, user, "追加主图", "batch_product", product.id, None,
                      {"rel_path": rel_path, "size": len(data), "fingerprint": digest, "sets_to_recheck": rechecked}, host)
            return self._response(session, batch, user)

    def remove(self, batch_id: int, product_id: int, actor: User, host: str) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            product, file = self._product(session, batch, product_id)
            fs = self.auth.pool.get(user.id)
            source = self.paths.join(Region.TEMP, batch.batch_no, self._folder(batch), file.rel_path)
            directory, name = file.rel_path.split("/", 1)
            recycled: str | None = None
            if fs.exists(source):
                target_dir = self.paths.join(Region.TEMP, batch.batch_no, RECYCLE, directory)
                if not fs.exists(target_dir):
                    fs.mkdir(target_dir, recursive=True)
                stem, extension = name.rsplit(".", 1)
                candidate, index = name, 1
                while fs.exists(target_dir + "/" + candidate):
                    index += 1
                    candidate = f"{stem}_{index}.{extension}"
                fs.rename(source, target_dir + "/" + candidate)
                recycled = f"{RECYCLE}/{directory}/{candidate}"
            file.removed = True
            self.ai.cancel_single(session, batch, product, "主图已移除", user.id, host)
            self.thumbs.invalidate(batch.id, file.id)
            affected: list[int] = []
            if product.kind == "item":
                for other in active_products(session, batch.id, "set"):
                    kept = [item for item in other.relation_items if not (isinstance(item, dict) and item.get("batch_product_id") == product.id)]
                    if len(kept) != len(other.relation_items):
                        other.relation_items = kept
                        affected.append(other.id)
            self._log(session, user, "移除主图", "batch_product", product.id,
                      {"rel_path": file.rel_path, "removed": False, "in_sets": affected},
                      {"removed": True, "recycle_path": recycled, "in_sets": []}, host)
            return self._response(session, batch, user)

    # ---- 提交与责任人 ----
    def submit(self, batch_id: int, note: str, actor: User, host: str) -> ReviewResponse:
        with self.auth.lock, self.auth.database.session() as session:
            batch, user = self._writable(session, batch_id, actor)
            note = note.strip()
            if len(note) > NOTE_MAX:
                raise AppError("value_too_long", f"备注最多 {NOTE_MAX} 字", 422, {"field": "note"})
            keys = {field.key for field in self._fields(session)}
            pending = self._pending(active_products(session, batch.id), keys)
            if not pending.can_submit:
                raise AppError("submit_blocked", "还有未处理的项，暂不能提交审核", 409, pending.model_dump())
            before = {"status": batch.status, "round": batch.round, "note_to_reviewer": batch.note_to_reviewer}
            if batch.status == REJECTED:
                batch.round += 1
            batch.status, batch.note_to_reviewer = SUBMITTED, note or None
            self._log(session, user, "提交审核", "batch", batch.id, before,
                      {"status": batch.status, "round": batch.round, "note_to_reviewer": batch.note_to_reviewer}, host)
            return self._response(session, batch, user)

    def change_owner(self, batch_id: int, user_id: int, actor: User, host: str) -> OwnerResponse:
        with self.auth.lock, self.auth.database.session() as session:
            admin = self._user(session, actor)
            if "admin" not in admin.roles:
                raise AppError("forbidden", "只有管理员可以更换责任人", 403)
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            target = session.get(User, user_id)
            if target is None:
                raise AppError("user_not_found", "用户不存在，请让该用户先登录一次", 404)
            if not target.enabled or not set(target.roles) & UPLOAD_ROLES:
                raise AppError("invalid_owner", "新责任人必须是启用且具有上传权限的账号", 422)
            before = {"owner_id": batch.owner_id}
            batch.owner_id = target.id
            self._log(session, admin, "更换批次责任人", "batch", batch.id, before, {"owner_id": target.id}, host)
            return OwnerResponse(id=batch.id, owner_id=target.id, owner_name=target.display_name or target.username)
