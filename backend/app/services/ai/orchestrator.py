"""AI 分阶段编排；只读取已脱敏送审缓存，不接触 NAS 原件。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets
import threading
import logging
import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AiAlert, Batch, BatchFile, BatchProduct, CompanyIndustryMap, Job, User, utcnow
from app.errors import AppError
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.nas import NasPermissionDenied, safe_join
from app.services.oplog import OplogService
from app.services.thumbs import ThumbService, is_main_image, source_version
from .client import AiCallError, AiClient, FakeAiClient
from .field_catalog import fixed_fields
from .parsing import FieldDefinition, parse_result
from .vision import has_visual_evidence
from .settings import AiSettingsService, require_admin

PROCESSING = "AI 处理中"
REVIEW = "待核对"
FAILED = "AI 处理失败"


class AiService:
    def __init__(self, auth: AuthService, queue: JobQueue, settings: AiSettingsService, cipher: SecretCipher,
                 thumbs: ThumbService) -> None:
        self.auth, self.queue, self.settings, self.cipher, self.thumbs = auth, queue, settings, cipher, thumbs
        self._clients: dict[tuple[object, ...], AiClient | FakeAiClient] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def client(self) -> AiClient | FakeAiClient:
        config = self.settings.effective()
        signature = (self.auth.config.mode, config.base_url, config.api_key_enc.get_secret_value(), config.model_vision,
                     config.model_text, config.model_embed, config.concurrency, config.timeout_s, config.retries)
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
        return batch if batch is not None and batch.status == PROCESSING and batch.payload.get("ai_run") == payload.get("run_id") else None

    @staticmethod
    def _log(session: Session, batch: Batch, action: str) -> None:
        now = utcnow()
        batch.updated_at = now
        batch.payload = {**batch.payload, "ai_last_progress": now.isoformat()}
        OplogService(session).write(batch.uploader_id, "system", action, "batch", str(batch.id), None,
                                   {"status": batch.status}, "成功", "AI 后台任务")

    def start_batch(self, session: Session, batch: Batch, actor: User, host: str) -> None:
        self.cancel_jobs(session, batch, "整批重新打标", actor.id, host)
        batch.status = PROCESSING
        run = secrets.token_hex(16)
        batch.payload = {**batch.payload, "ai_run": run, "ai_initialized": False, "ai_jobs": {},
                         "ai_last_progress": utcnow().isoformat()}
        batch.recommended_path = None
        for product in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id)):
            product.ai_status, product.ai_error = "pending", None
        for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id, BatchFile.upload_status == "done")):
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
        company = str(batch.payload["folder_name"]).split("_")[1]
        matches = [row for row in session.scalars(select(CompanyIndustryMap)) if row.company_name == company or company in row.aliases]
        if len(matches) != 1:
            return None
        row = matches[0]
        try:
            return safe_join("01已中标项目" if batch.won else "02未中标项目",
                             f"{row.industry_seq:02d}{row.industry_name}", f"{row.company_seq:02d}{row.company_name}")
        except NasPermissionDenied:
            return None

    def handle_batch(self, context: JobContext) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            if not batch.payload.get("ai_initialized"):
                fields = [{"key": row.key, "label": row.label, "hint": row.hint} for row in fixed_fields(session)]
                batch.payload = {**batch.payload, "ai_fields": fields, "ai_initialized": True}
                batch.recommended_path = self._recommend(session, batch)
                for file in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.id)):
                    if not is_main_image(file) or file.removed:
                        continue
                    product = session.scalar(select(BatchProduct).where(BatchProduct.batch_id == batch.id, BatchProduct.batch_file_id == file.id))
                    if product is None:
                        product = BatchProduct(batch_id=batch.id, batch_file_id=file.id,
                            kind="set" if file.rel_path.startswith("套装/") else "item", display_name=Path(file.rel_path).stem)
                        session.add(product)
                    product.ai_status, product.ai_error = "pending", None
                    if not has_visual_evidence(product, file):
                        product.ai_success_at = product.ai_model = product.ai_fingerprint = None
                    product.flags, product.relation_items, product.relation_confirmed = [], [], False
                self._log(session, batch, "建立 AI 商品清单及位置建议")
            session.flush()
            self._advance(session, batch)

    def _advance(self, session: Session, batch: Batch) -> None:
        if batch.status != PROCESSING or not batch.payload.get("ai_initialized"):
            return
        session.flush()
        products = list(session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id).order_by(BatchProduct.id)))
        items = [product for product in products if product.kind == "item"]
        phase = items if any(product.ai_status == "pending" for product in items) else [product for product in products if product.kind == "set"]
        references = dict(batch.payload.get("ai_jobs", {}))
        for product in phase:
            if product.ai_status != "pending" or str(product.id) in references:
                continue
            file = session.get(BatchFile, product.batch_file_id)
            if file is None or file.removed or file.upload_status != "done" or file.fingerprint_kind != "full" or not file.fingerprint:
                product.ai_status, product.ai_error = "failed", "主图缺少有效的完整 SHA-256，请核对文件后重试"
                self._log(session, batch, "主图指纹未验证")
            elif file.thumb_status == "failed":
                product.ai_status, product.ai_error = "failed", file.thumb_error or "送审图生成失败"
                self._log(session, batch, "送审图失败，跳过该商品")
            elif file.thumb_status == "done" and file.thumb_fingerprint != file.fingerprint:
                product.ai_status, product.ai_error = "failed", "旧送审图未经完整指纹校验，请重试"
                self._log(session, batch, "送审图指纹未验证")
            elif file.thumb_status == "done":
                job = self.queue.enqueue("ai_tag_product", {"batch_id": batch.id, "run_id": batch.payload["ai_run"],
                    "product_id": product.id}, session=session)
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
            self._log(session, batch, "AI 商品处理结束")
            self._advance(session, batch)

    def _sanitize(self, session: Session, batch: Batch) -> Callable[[str], str]:
        user = session.get(User, batch.uploader_id)
        nas = self.auth.config.nas
        secrets_ = [batch.project_code, str(batch.payload["folder_name"]).split("_")[1], user.username,
                    nas.host, nas.share, nas.root_path, nas.temp_path, nas.backup_path]
        company = secrets_[1]
        for row in session.scalars(select(CompanyIndustryMap)):
            if row.company_name == company or company in row.aliases:
                secrets_.extend([row.company_name, *row.aliases])
        def clean(value: str) -> str:
            for secret in sorted(filter(None, secrets_), key=len, reverse=True):
                value = re.sub(re.escape(secret), "[已省略]", value, flags=re.IGNORECASE)
            return value
        return clean

    def handle_product(self, context: JobContext) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            product = session.get(BatchProduct, context.job.payload["product_id"])
            if product is None or product.ai_status == "done":
                return
            file = session.get(BatchFile, product.batch_file_id)
            if file is None or file.removed or file.upload_status != "done" or file.fingerprint_kind != "full" or not file.fingerprint:
                raise JobFailure("主图缺少有效的完整 SHA-256", retryable=False)
            version = source_version(file)
            image_path = self.auth.config.paths.data_dir / "thumbs" / "batch" / str(batch.id) / str(file.id) / "1024.jpg"
            if file.thumb_status != "done" or file.thumb_fingerprint != file.fingerprint or not image_path.is_file():
                raise JobFailure("送审图尚不可用，请重新登录恢复缩略图后重试", retryable=False)
            image = image_path.read_bytes()
            saved = {field["key"]: field["hint"] for field in batch.payload.get("ai_fields", [])}
            fields: list[FieldDefinition] = [
                {"key": row.key, "label": row.label, "hint": saved.get(row.key, row.hint)}
                for row in fixed_fields(session)
            ]
            fixed_keys = {field["key"] for field in fields}
            clean = self._sanitize(session, batch)
            safe_fields = [{"key": field["key"], "label": clean(field["label"]), "hint": clean(field["hint"])} for field in fields]
            items, valid = [], {}
            if product.kind == "set":
                for index, item in enumerate(session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id,
                    BatchProduct.kind == "item").order_by(BatchProduct.id)), 1):
                    valid[index] = item.id
                    items.append({"index": index, "name": clean(item.display_name), "tags": {key: clean(str(value.get("value", "")))
                        for key, value in item.tags.items() if isinstance(value, dict) and key in fixed_keys}})
            batch_id, product_id, kind, filename = batch.id, product.id, product.kind, file.rel_path.rsplit("/", 1)[-1]
        client = self.client()
        retries = client.config.retries
        for attempt in range(retries + 1):
            try:
                raw = client.generate(image, safe_fields, items=items if kind == "set" else None,
                                      filename=filename, batch_id=batch_id, product_id=product_id)
            except AiCallError as error:
                raise JobFailure(error.message, retryable=False) from None
            try:
                result = parse_result(raw, fields, kind=kind, valid_relations=valid)
                break
            except ValueError:
                if attempt == retries:
                    raise JobFailure("模型未返回可解析的 JSON", retryable=False) from None
        with self.auth.lock, self.auth.database.session() as session:
            batch = self._current(session, context.job.payload)
            if batch is None:
                return
            product = session.get(BatchProduct, product_id)
            if product is None:
                return
            file = session.get(BatchFile, product.batch_file_id)
            if (file is None or file.removed or file.upload_status != "done" or file.fingerprint_kind != "full"
                    or file.thumb_fingerprint != file.fingerprint or source_version(file) != version):
                raise JobFailure("主图已变化，请重新识别", retryable=False)
            product.ai_success_at = utcnow()
            product.ai_model = client.config.model_vision or "fake-vision"
            product.ai_fingerprint = file.fingerprint
            product.tags = {**product.tags, **result["tags"]}
            product.free_text, product.flags = result["free_text"], result["flags"]
            product.relation_items, product.relation_confirmed = result["relation_items"], False
            product.ai_status, product.ai_error = "done", None

    def check_stalls(self) -> None:
        minutes = self.settings.effective().stall_alert_min
        with self.auth.lock, self.auth.database.session() as session:
            for batch in session.scalars(select(Batch).where(Batch.status == PROCESSING)):
                run = batch.payload.get("ai_run")
                if not run:
                    continue
                last = datetime.fromisoformat(batch.payload.get("ai_last_progress", batch.updated_at.isoformat()))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (utcnow() - last).total_seconds() > minutes * 60 and session.scalar(select(AiAlert.id).where(AiAlert.batch_id == batch.id, AiAlert.run_id == run)) is None:
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
