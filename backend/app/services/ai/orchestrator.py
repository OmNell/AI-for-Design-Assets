"""AI 分阶段编排；只读取已脱敏送审缓存，不接触 NAS 原件。"""
from __future__ import annotations

from datetime import datetime, timezone
import secrets
import threading
import logging
import re
from collections.abc import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import AiConfig
from app.db.models import AiAlert, Batch, BatchFile, BatchProduct, CompanyIndustryMap, Job, User, utcnow
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.oplog import OplogService
from app.services.thumbs import ThumbService, source_version
from .client import AiCallError, AiClient, FakeAiClient
from .field_catalog import fixed_fields
from .parsing import FieldDefinition, parse_result
from . import pre_descriptions as descriptions
from .vision import has_visual_evidence
from app.services.admin.company_map import recommend
from .settings import AiSettingsService, require_admin

PROCESSING = "AI 处理中"
REVIEW = "待核对"
FAILED = "AI 处理失败"


REVIEWING = (REVIEW, "已驳回")
RELATION_FLAGS = ("empty_relation", "relation_recheck")
HUMAN_SOURCES = ("manual", "ai_confirmed")


def ai_run_stalled(batch: Batch, now: datetime, minutes: int) -> bool:
    raw = batch.payload.get("ai_last_progress")
    last = datetime.fromisoformat(raw) if isinstance(raw, str) else batch.updated_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() > minutes * 60


def active_products(session: Session, batch_id: int, kind: str | None = None) -> list[BatchProduct]:
    """核对阶段移除的主图保留记录但不再参与打标、展示与提交。"""
    query = select(BatchProduct).join(BatchFile, BatchFile.id == BatchProduct.batch_file_id).where(
        BatchProduct.batch_id == batch_id, BatchFile.removed.is_not(True),
        or_(BatchFile.conflict.is_(None), BatchFile.conflict != "duplicate")).order_by(BatchProduct.id)
    if kind is not None:
        query = query.where(BatchProduct.kind == kind)
    return list(session.scalars(query))


class AiService:
    def __init__(self, auth: AuthService, queue: JobQueue, settings: AiSettingsService, cipher: SecretCipher,
                 thumbs: ThumbService) -> None:
        self.auth, self.queue, self.settings, self.cipher, self.thumbs = auth, queue, settings, cipher, thumbs
        self._clients: dict[tuple[object, ...], AiClient | FakeAiClient] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def client(self, config: AiConfig | None = None) -> AiClient | FakeAiClient:
        config = config if config is not None else self.settings.effective()
        signature = (self.auth.config.mode, config.base_url, config.api_key_enc.get_secret_value(), config.model_vision,
                     config.model_text, config.model_text_version, config.model_embed, config.model_embed_version, config.concurrency, config.timeout_s, config.retries)
        with self.auth.lock:
            if signature not in self._clients:
                factory = FakeAiClient if self.auth.config.mode == "dev" else AiClient
                self._clients[signature] = factory(config, self.auth.database, self.cipher)
            client = self._clients[signature]
            client.activate_limit()
            return client

    @staticmethod
    def _current(session: Session, payload: object) -> Batch | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("batch_id"), int):
            return None
        batch = session.get(Batch, payload["batch_id"])
        if batch is None:
            return None
        if payload.get("single"):
            # 核对阶段的单张打标：批次状态不变，以每个商品的令牌区分新旧请求。
            entry = batch.payload.get("ai_single", {}).get(str(payload.get("product_id")))
            valid = batch.status in REVIEWING and isinstance(entry, dict) and entry.get("token") == payload["single"]
            return batch if valid else None
        return batch if batch.status == PROCESSING and batch.payload.get("ai_run") == payload.get("run_id") else None

    @staticmethod
    def _log(session: Session, batch: Batch, action: str) -> None:
        now = utcnow()
        batch.updated_at = now
        batch.payload = {**batch.payload, "ai_last_progress": now.isoformat()}
        OplogService(session).write(batch.uploader_id, "system", action, "batch", str(batch.id), None,
                                   {"status": batch.status}, "成功", "AI 后台任务")

    def start_batch(self, session: Session, batch: Batch, actor: User, host: str, *, session_id: int | None = None) -> None:
        products = descriptions.products_for_batch(session, batch, initialize=True)
        descriptions.require_descriptions(products)
        self.cancel_jobs(session, batch, "整批重新打标", actor.id, host)
        batch.status = PROCESSING
        run = secrets.token_hex(16)
        batch.payload = {**batch.payload, "ai_run": run, "ai_initialized": False, "ai_jobs": {}, "ai_single": {},
                         "ai_last_progress": utcnow().isoformat()}
        if batch.kind == "ingest":
            batch.recommended_path = None
        for product, file in products:
            product.ai_status, product.ai_error = "pending", None
            if session_id is not None and actor.id in (batch.owner_id, batch.uploader_id):
                self.thumbs.enqueue(session, batch, file, actor_id=actor.id, session_id=session_id, retry=True)
            else:
                self.thumbs.enqueue(session, batch, file)
        self.queue.enqueue("ai_tag_batch", {"batch_id": batch.id, "run_id": run}, session=session)
        self._log(session, batch, "开始 AI 打标")

    def cancel_jobs(self, session: Session, batch: Batch, reason: str, user_id: int, host: str) -> None:
        for job in session.scalars(select(Job).where(Job.kind.in_(["ai_tag_batch", "ai_tag_product"]), Job.status == "queued")):
            if isinstance(job.payload, dict) and job.payload.get("batch_id") == batch.id:
                job.status, job.error, job.finished_at = "failed", reason, utcnow()
                OplogService(session).write(user_id, "uploader", "取消待执行 AI 任务", "job", str(job.id),
                                           {"status": "queued"}, {"status": "failed", "reason": reason}, "成功", host)

    @staticmethod
    def _recommend(session: Session, batch: Batch) -> str | None:
        return recommend(session, batch)

    def handle_batch(self, context: JobContext) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            if not batch.payload.get("ai_initialized"):
                fields = [{"key": row.key, "label": row.label, "hint": row.hint} for row in fixed_fields(session)]
                batch.payload = {**batch.payload, "ai_fields": fields, "ai_initialized": True}
                if batch.kind == "ingest":
                    batch.recommended_path = self._recommend(session, batch)
                for product, file in descriptions.products_for_batch(session, batch, initialize=True):
                    # 升级前启动的批次保留已成功的结果，尚未处理的图片仍需预描述。
                    if product.ai_status != "done" or not has_visual_evidence(product, file):
                        product.ai_status, product.ai_error = "pending", None
                    if not product.relation_confirmed:
                        product.relation_confirm_kind = "pending"
                self._log(session, batch, "建立 AI 商品清单及位置建议")
            session.flush()
            self._advance(session, batch)

    def _advance(self, session: Session, batch: Batch) -> None:
        if batch.status != PROCESSING or not batch.payload.get("ai_initialized"):
            return
        session.flush()
        products = active_products(session, batch.id)
        items = [product for product in products if product.kind == "item"]
        phase = items if any(product.ai_status == "pending" for product in items) else [product for product in products if product.kind == "set"]
        references = dict(batch.payload.get("ai_jobs", {}))
        for product in phase:
            if product.ai_status != "pending" or str(product.id) in references:
                continue
            file = session.get(BatchFile, product.batch_file_id)
            if file is None or not descriptions.is_completed_main(file) or not descriptions.valid_fingerprint(file):
                product.ai_status, product.ai_error = "failed", "主图缺少有效的完整 SHA-256，请核对文件后重试"
                self._log(session, batch, "主图指纹未验证")
            elif descriptions.state(product, file) != "ready":
                product.ai_status, product.ai_error = "failed", "请先填写或重新确认主图预描述，再启动 AI"
                self._log(session, batch, "主图预描述待确认")
            elif file.thumb_status == "failed":
                product.ai_status, product.ai_error = "failed", file.thumb_error or "送审图生成失败"
                self._log(session, batch, "送审图失败，跳过该商品")
            elif file.thumb_status == "done" and file.thumb_fingerprint != file.fingerprint:
                product.ai_status, product.ai_error = "failed", "旧送审图未经完整指纹校验，请重试"
                self._log(session, batch, "送审图指纹未验证")
            elif file.thumb_status == "done":
                job = self.queue.enqueue("ai_tag_product", {"batch_id": batch.id, "run_id": batch.payload["ai_run"],
                    "product_id": product.id, "description_version": product.pre_description_version,
                    "fingerprint": file.fingerprint}, session=session)
                references[str(product.id)] = job.id
        batch.payload = {**batch.payload, "ai_jobs": references}
        if phase is items and items and all(product.ai_status != "pending" for product in items):
            self._advance(session, batch)
            return
        if all(product.ai_status != "pending" for product in products):
            batch.status = REVIEW if any(product.ai_status == "done" for product in products) else FAILED
            self._log(session, batch, "AI 打标结束")

    def thumb_changed(self, session: Session, job: Job) -> None:
        if isinstance(job.payload, dict):
            batch = session.get(Batch, job.payload.get("batch_id"))
            if batch is not None:
                self._advance(session, batch)
                self._advance_single(session, batch)

    def start_single(self, session: Session, batch: Batch, product: BatchProduct, actor: User, host: str, session_id: int) -> None:
        """核对阶段单张打标；调用方持有认证锁并已完成权限与状态检查。"""
        file = session.get(BatchFile, product.batch_file_id)
        if file is None or not descriptions.is_completed_main(file):
            raise AppError("product_not_found", "找不到已上传的主图", 404)
        descriptions.require_descriptions([(product, file)])
        self.cancel_single(session, batch, product, "重新打标", actor.id, host)
        product.ai_status, product.ai_error = "pending", None
        singles = dict(batch.payload.get("ai_single", {}))
        singles[str(product.id)] = {"token": secrets.token_hex(16), "job": None,
            "description_version": product.pre_description_version, "fingerprint": file.fingerprint}
        batch.payload = {**batch.payload, "ai_single": singles}
        self.thumbs.enqueue(session, batch, file, actor_id=actor.id, session_id=session_id, retry=True)
        self._advance_single(session, batch)

    def cancel_single(self, session: Session, batch: Batch, product: BatchProduct, reason: str, user_id: int, host: str) -> None:
        singles = dict(batch.payload.get("ai_single", {}))
        entry = singles.pop(str(product.id), None)
        if entry is None:
            return
        # 先作废令牌，再取消任务，避免状态回调把商品记为失败。
        batch.payload = {**batch.payload, "ai_single": singles}
        job = session.get(Job, entry.get("job")) if isinstance(entry, dict) and isinstance(entry.get("job"), int) else None
        if job is not None and job.status == "queued":
            job.status, job.error, job.finished_at = "failed", reason, utcnow()
            OplogService(session).write(user_id, "uploader", "取消待执行 AI 任务", "job", str(job.id),
                                       {"status": "queued"}, {"status": "failed", "reason": reason}, "成功", host)

    def _advance_single(self, session: Session, batch: Batch) -> None:
        if batch.status not in REVIEWING or not batch.payload.get("ai_single"):
            return
        session.flush()
        singles = dict(batch.payload["ai_single"])
        changed = False
        for key, entry in list(singles.items()):
            if not isinstance(entry, dict) or entry.get("job") is not None:
                continue
            product = session.get(BatchProduct, int(key))
            file = session.get(BatchFile, product.batch_file_id) if product is not None else None
            if file is None or file.removed or product.ai_status != "pending":
                singles.pop(key)
            elif (descriptions.state(product, file) != "ready" or entry.get("description_version") != product.pre_description_version
                  or entry.get("fingerprint") != file.fingerprint):
                product.ai_status, product.ai_error = "failed", "主图或预描述已变化，请重新确认后识别"
                singles.pop(key)
            elif file.thumb_status == "failed":
                product.ai_status, product.ai_error = "failed", file.thumb_error or "送审图生成失败"
                singles.pop(key)
            elif file.thumb_status == "done":
                job = self.queue.enqueue("ai_tag_product", {"batch_id": batch.id, "run_id": batch.payload.get("ai_run"),
                    "product_id": product.id, "single": entry["token"], "description_version": entry["description_version"],
                    "fingerprint": entry["fingerprint"]}, session=session)
                singles[key] = {**entry, "job": job.id}
            else:
                continue
            changed = True
        if changed:
            batch.payload = {**batch.payload, "ai_single": singles}

    def job_changed(self, session: Session, job: Job) -> None:
        batch = self._current(session, job.payload)
        if batch is None:
            return
        if job.kind == "ai_tag_batch":
            if job.status == "failed":
                batch.status = FAILED
                self._log(session, batch, "AI 批次编排失败")
            return
        product = session.get(BatchProduct, job.payload["product_id"])
        if product is None:
            return
        if job.status == "failed":
            product.ai_status, product.ai_error = "failed", job.error
        if job.status in ("done", "failed"):
            if job.payload.get("single"):
                singles = dict(batch.payload.get("ai_single", {}))
                singles.pop(str(product.id), None)
                batch.payload = {**batch.payload, "ai_single": singles}
                self._log(session, batch, "AI 单张处理结束")
                return
            self._log(session, batch, "AI 商品处理结束")
            self._advance(session, batch)

    def _sanitize(self, session: Session, batch: Batch, described_by: int | None) -> Callable[[str], str]:
        nas = self.auth.config.nas
        company = str(batch.payload["folder_name"]).split("_")[1]
        secrets_ = [batch.project_code, company, batch.batch_no, nas.host, nas.share,
                    nas.root_path, nas.temp_path, nas.backup_path, nas.system_user]
        for user_id in {batch.uploader_id, batch.owner_id, described_by} - {None}:
            if (user := session.get(User, user_id)) is not None:
                secrets_.append(user.username)
        for row in session.scalars(select(CompanyIndustryMap)):
            if company.casefold() in {name.casefold() for name in [row.company_name, *row.aliases]}:
                secrets_.extend([row.company_name, *row.aliases])
        tokens = sorted(set(filter(None, secrets_)), key=len, reverse=True)
        private = re.compile("|".join(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])" if re.fullmatch(r"[a-zA-Z0-9_-]+", token)
            else re.escape(token) for token in tokens), re.IGNORECASE)
        # 路径和网址不是识别所需的产品属性。
        path = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|[A-Za-z]:[\\/]|[\\/]{2}|(?<!\w)/)[^\s，。；、！？]+|"
                          r"[^\s，。；、！？/\\]+(?:[\\/][^\s，。；、！？/\\]+){2,}")
        def clean(value: str) -> str:
            return private.sub("[已省略]", path.sub("[已省略]", value))
        return clean

    @staticmethod
    def _description_input(job: Job, product: BatchProduct, file: BatchFile) -> str:
        if descriptions.state(product, file) != "ready":
            raise JobFailure("请先填写或重新确认主图预描述，再启动 AI", retryable=False)
        if (job.payload.get("description_version") != product.pre_description_version
                or job.payload.get("fingerprint") != file.fingerprint):
            raise JobFailure("主图或预描述已变化，请重新确认后识别", retryable=False)
        return product.pre_description or ""

    def handle_product(self, context: JobContext) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            product = session.get(BatchProduct, context.job.payload["product_id"])
            if product is None or product.batch_id != batch.id or product.ai_status == "done":
                return
            file = session.get(BatchFile, product.batch_file_id)
            if file is None or file.batch_id != batch.id or not descriptions.is_completed_main(file) or not descriptions.valid_fingerprint(file):
                raise JobFailure("主图缺少有效的完整 SHA-256", retryable=False)
            version = source_version(file)
            image_path = self.auth.config.paths.data_dir / "thumbs" / "batch" / str(batch.id) / str(file.id) / "1024.jpg"
            if file.thumb_status != "done" or file.thumb_fingerprint != file.fingerprint or not image_path.is_file():
                raise JobFailure("送审图尚不可用，请重新登录恢复缩略图后重试", retryable=False)
            image = image_path.read_bytes()
            saved = {field["key"]: field["hint"] for field in batch.payload.get("ai_fields", [])}
            fields: list[FieldDefinition] = [
                {"key": row.key, "label": row.label, "hint": row.hint if context.job.payload.get("single") else saved.get(row.key, row.hint)}
                for row in fixed_fields(session)
            ]
            fixed_keys = {field["key"] for field in fields}
            pre_description = self._description_input(context.job, product, file)
            clean = self._sanitize(session, batch, product.pre_description_by)
            pre_description = clean(pre_description)
            if not any(char.isalnum() for char in pre_description.replace("[已省略]", "")):
                raise JobFailure("主图预描述隐去保密信息后没有产品内容，请改写后重试", retryable=False)
            safe_fields = [{"key": field["key"], "label": clean(field["label"]), "hint": clean(field["hint"])} for field in fields]
            items, valid = [], {}
            if product.kind == "set":
                for index, item in enumerate(active_products(session, batch.id, "item"), 1):
                    valid[index] = item.id
                    items.append({"index": index, "name": clean(item.display_name), "tags": {key: clean(str(value.get("value", "")))
                        for key, value in item.tags.items() if isinstance(value, dict) and key in fixed_keys}})
            batch_id, product_id, kind, filename = batch.id, product.id, product.kind, file.rel_path.rsplit("/", 1)[-1]
        client = self.client()
        retries = client.config.retries
        for attempt in range(retries + 1):
            try:
                raw = client.generate(image, safe_fields, items=items if kind == "set" else None,
                                      filename=filename, batch_id=batch_id, product_id=product_id, pre_description=pre_description)
            except AiCallError as error:
                raise JobFailure(error.message, retryable=False) from None
            try:
                result = parse_result(raw, fields, kind=kind, valid_relations=valid)
                break
            except ValueError:
                if attempt == retries:
                    raise JobFailure("模型未返回可解析的 JSON", retryable=False) from None
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            product = session.get(BatchProduct, product_id)
            if product is None:
                return
            file = session.get(BatchFile, product.batch_file_id)
            if (file is None or not descriptions.is_completed_main(file)
                    or file.thumb_fingerprint != file.fingerprint or source_version(file) != version):
                raise JobFailure("主图已变化，请重新识别", retryable=False)
            self._description_input(context.job, product, file)
            description_changed = product.ai_description_version != product.pre_description_version
            product.ai_success_at = utcnow()
            product.ai_description_version = product.pre_description_version
            product.ai_raw_result = raw
            product.ai_model = client.config.model_vision or "fake-vision"
            product.ai_fingerprint = file.fingerprint
            for key, tag in result["tags"].items():
                previous = product.tags.get(key, {})
                if previous.get("source") == "manual" or (not description_changed and previous.get("source") in HUMAN_SOURCES):
                    result["tags"][key] = {**previous, "ai_value": tag["ai_value"]}
            product.tags = {**product.tags, **result["tags"]}
            product.ai_free_text = result["free_text"]
            if product.free_text_source != "manual":
                product.free_text = product.ai_free_text
            flags = list(result["flags"])
            if product.kind == "set":
                live_ids = {item.id for item in active_products(session, batch.id, "item")}
                if not product.relation_confirmed:
                    product.relation_items = [r for r in result["relation_items"] if r["batch_product_id"] in live_ids]
                if product.relation_confirmed:
                    flags = [flag for flag in flags if flag != "empty_relation"]
                elif "relation_recheck" in product.flags:
                    flags.append("relation_recheck")
            product.flags = flags
            product.ai_status, product.ai_error = "done", None

    def check_stalls(self) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            minutes = self.settings._effective(session).stall_alert_min
            now = utcnow()
            for batch in session.scalars(select(Batch).where(Batch.status == PROCESSING)):
                run = batch.payload.get("ai_run")
                if not run:
                    continue
                if ai_run_stalled(batch, now, minutes) and session.scalar(
                        select(AiAlert.id).where(AiAlert.batch_id == batch.id, AiAlert.run_id == run)) is None:
                    session.add(AiAlert(batch_id=batch.id, run_id=run, reason=f"AI 处理中超过 {minutes} 分钟无进展"))
                    OplogService(session).write(None, "system", "AI 处理停滞提醒", "batch", str(batch.id), None,
                                               {"reason": "AI 处理停滞"}, "失败", "后台检测")

    def alerts(self, actor: User) -> list[AiAlert]:
        with self.auth.lock, self.auth.database.session() as session:
            require_admin(session, actor)
            return list(session.scalars(select(AiAlert).order_by(AiAlert.id.desc())))

    def test_models(self, actor: User, host: str) -> list[dict[str, object]]:
        self.settings.read(actor)
        results = self.client().test_models()
        with self.auth.lock, self.auth.database.session() as session:
            OplogService(session).write(actor.id, "admin", "测试 AI 模型连接", "settings", "ai", None,
                {"results": [{"kind": result["kind"], "success": result["success"]} for result in results]}, "成功", host)
        return results

    def start(self) -> None:
        def monitor() -> None:
            while not self._stop.wait(5):
                try:
                    self.check_stalls()
                except Exception:
                    logging.getLogger("nas_workbench").error("AI 停滞检测暂时失败，将在下次检查继续")
        self._thread = threading.Thread(target=monitor, name="ai-stall-monitor", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def close_clients(self) -> None:
        # 必须在工作线程结束之后关闭，避免中断在途请求。
        for client in self._clients.values():
            client.close()
        self._clients.clear()
