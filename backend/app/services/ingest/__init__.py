"""入库执行器：以终审本人的 NAS 会话执行 9 步，每步先查「是否已完成」，可从任意一步重跑。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchFile, BatchProduct, CatalogFile, CompanyIndustryMap, Industry, Job, Product, ProductRelation,
                           ProductTag, Project, User, utcnow)
from app.errors import AppError
from app.services import archive
from app.services.ai.settings import AiSettingsService
from app.services.auth.service import AuthService
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.links import ensure_link
from app.services.nas import NasAlreadyExists, NasFs, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, Region
from app.services.nas.factory import create_paths
from app.services.notify import NotifyService
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED

INGESTING, BROKEN, DONE = "入库中", "入库异常", "已入库"
STEPS = {1: "检查目标位置", 2: "创建行业与企业文件夹", 3: "移动项目文件夹", 4: "发放商品编号", 5: "写单品与套装档案",
         6: "写项目档案", 7: "登记卡片柜", 8: "生成永久链接", 9: "完成入库"}
POSTOP_STEPS = {1: "生成执行计划", 2: "移动与写入文件", 3: "更新卡片柜", 4: "更新档案", 5: "完成"}
logger = logging.getLogger("nas_workbench")


def steps_of(kind: str) -> dict[int, str]:
    """首次入库走 9 步；入库后操作统一为 5 步。"""
    return STEPS if kind == "ingest" else POSTOP_STEPS


class IngestError(Exception):
    """可以直接展示给终审的中文原因。"""


def category_of(rel_path: str) -> str:
    head = rel_path.split("/")[0] if "/" in rel_path else ""
    return head if head in REQUIRED or head in ("套装", "单品") else "other"


class IngestService:
    def __init__(self, auth: AuthService, queue: JobQueue, notify: NotifyService, ai_settings: AiSettingsService) -> None:
        self.auth, self.queue, self.notify, self.ai_settings = auth, queue, notify, ai_settings
        self.paths = create_paths(auth.config)
        self.thumbs = auth.config.paths.data_dir / "thumbs"
        # 仅供故障注入测试：在完成指定步骤后强制中断。
        self.fault_after: int | None = None

    # ---- 派发与继续 ----
    def enqueue(self, session: Session, batch: Batch, reviewer: User) -> Job:
        """终审通过或点「继续」时调用；任务以终审本人的 NAS 会话执行。"""
        return self.queue.enqueue("ingest" if batch.kind == "ingest" else "postop", {"batch_id": batch.id}, run_as_user_id=reviewer.id, session=session)

    def resume(self, batch_id: int, actor: User, host: str) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            user = session.get(User, actor.id)
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise AppError("batch_not_found", "批次不存在", 404)
            if user is None or not user.enabled or "final_reviewer" not in user.roles or batch.final_reviewer_id != user.id:
                raise AppError("forbidden", "只有本批次的终审可以继续入库", 403)
            if batch.status != BROKEN:
                raise AppError("invalid_batch_state", "批次不在入库异常状态", 409)
            before = {"status": batch.status, "ingest_step": batch.ingest_step, "ingest_error": batch.ingest_error}
            batch.status, batch.ingest_error = INGESTING, None
            self.enqueue(session, batch, user)
            OplogService(session).write(user.id, ",".join(user.roles), "继续入库", "batch", str(batch.id), before,
                                       {"status": batch.status, "ingest_step": batch.ingest_step}, "成功", host)

    # ---- 任务入口 ----
    def _fail(self, batch_id: int, step: int, reason: str) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch is None or batch.status != INGESTING:
                return
            batch.status, batch.ingest_error = BROKEN, f"第 {step} 步（{steps_of(batch.kind).get(step, '')}）：{reason}"
            OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "入库中断", "batch", str(batch.id),
                {"status": INGESTING, "ingest_step": batch.ingest_step}, {"status": BROKEN, "step": step, "reason": reason}, "失败", "入库任务")
            self.notify.emit(session, "ingest_failed", batch)

    def job_changed(self, session: Session, job: Job) -> None:
        """处理函数没机会运行（终审会话已失效、账号停用、服务重启）时，同样把批次标为入库异常。"""
        if job.status != "failed" or not isinstance(job.payload, dict):
            return
        batch = session.get(Batch, job.payload.get("batch_id"))
        if batch is not None and batch.status == INGESTING:
            step = batch.ingest_step + 1
            batch.status, batch.ingest_error = BROKEN, f"第 {step} 步（{steps_of(batch.kind).get(step, '')}）：{job.error or '任务未能执行'}"
            OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "入库中断", "batch", str(batch.id),
                {"status": INGESTING, "ingest_step": batch.ingest_step}, {"status": BROKEN, "step": step, "reason": job.error}, "失败", "入库任务")
            self.notify.emit(session, "ingest_failed", batch)

    def handle(self, context: JobContext) -> None:
        batch_id = context.job.payload["batch_id"]
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch is None or batch.kind != "ingest" or batch.status != INGESTING or batch.final_reviewer_id != context.job.run_as_user_id:
                return
        if context.fs is None:
            raise JobFailure("操作人会话已失效", retryable=False)
        self.run_steps(batch_id, context.fs, {1: self._check_target, 2: self._make_parents, 3: self._move, 4: self._assign_codes,
            5: self._write_product_archives, 6: self._write_project_archive, 7: self._register, 8: self._links, 9: self._finish})

    def run_steps(self, batch_id: int, fs: NasFs, steps: dict[int, Callable[[int, NasFs], None]]) -> None:
        """按顺序执行各步并记录进度；首次入库和入库后操作共用。任一步失败即把批次标为入库异常。"""
        with self.auth.lock, self.auth.database.session() as session:
            names = steps_of(session.get(Batch, batch_id).kind)
        for number, action in steps.items():
            try:
                # 每一步都重新执行自己的「是否已完成」检查，已完成的直接跳过。
                action(batch_id, fs)
            except IngestError as error:
                reason = str(error)
            except NasPermissionDenied:
                reason = "NAS 无权限：需要终审本人对正式目录的写权限"
            except NasSessionExpired:
                reason = "操作人会话已失效"
            except NasNotFound:
                reason = "NAS 文件或目录不存在"
            except NasAlreadyExists:
                reason = "NAS 上已存在同名文件或目录"
            except NasUnavailable:
                reason = "NAS 暂时不可用"
            except Exception:
                logger.error("入库第 %d 步执行失败", number)
                reason = "执行失败，请检查运行环境后继续"
            else:
                with self.auth.lock, self.auth.database.session() as session:
                    batch = session.get(Batch, batch_id)
                    if batch.ingest_step < number:
                        batch.ingest_step = number
                        OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "入库步骤完成", "batch", str(batch.id),
                                                   {"ingest_step": number - 1}, {"ingest_step": number, "name": names[number]}, "成功", "入库任务")
                if self.fault_after == number:
                    self.fault_after = None
                    self._fail(batch_id, number + 1, "故障注入：强制中断")
                    raise JobFailure("故障注入：强制中断", retryable=False)
                continue
            self._fail(batch_id, number, reason)
            raise JobFailure(reason, retryable=False)

    # ---- 路径 ----
    def _facts(self, session: Session, batch_id: int) -> tuple[Batch, str, str, str]:
        batch = session.get(Batch, batch_id)
        if not batch.final_path:
            raise IngestError("存放位置尚未确定")
        folder = str(batch.payload["folder_name"])
        source = self.paths.join(Region.TEMP, batch.batch_no, folder)
        target = self.paths.join(Region.FORMAL, batch.final_path, folder)
        return batch, folder, source, target

    def _locations(self, batch_id: int) -> tuple[str, str, str, str, str]:
        with self.auth.lock, self.auth.database.session() as session:
            batch, folder, source, target = self._facts(session, batch_id)
            return folder, source, target, str(batch.final_path), batch.project_code

    # ---- 9 步 ----
    def _check_target(self, batch_id: int, fs: NasFs) -> None:
        folder, source, target, final_path, code = self._locations(batch_id)
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            project = session.scalar(select(Project).where(Project.project_code == code))
            if project is not None and project.id != batch.project_id:
                raise IngestError(f"立项编号 {code} 已有入库项目")
            moved = batch.payload.get("ingest_target") == target
        parent = target.rsplit("/", 1)[0]
        if fs.exists(target):
            # 目标已存在只有一种合法情形：本批次此前已完成移动。
            if not moved or fs.exists(source):
                raise IngestError("目标位置已存在同名项目文件夹")
        if fs.exists(parent):
            others = [name for name in fs.list(parent) if name != folder and name.split("_")[0] == code]
            if others:
                raise IngestError(f"目标位置已存在同编号的项目文件夹：{others[0]}")

    def _make_parents(self, batch_id: int, fs: NasFs) -> None:
        _, _, _, final_path, _ = self._locations(batch_id)
        current = self.paths.root(Region.FORMAL)
        for part in final_path.split("/"):
            current = f"{current}/{part}"
            if not fs.exists(current):
                fs.mkdir(current)

    def _move(self, batch_id: int, fs: NasFs) -> None:
        _, source, target, _, _ = self._locations(batch_id)
        has_source, has_target = fs.exists(source), fs.exists(target)
        if not has_source and has_target:
            return
        if has_source and has_target:
            raise IngestError("目标位置已存在同名项目文件夹")
        if not has_source:
            raise IngestError("临时区找不到本批次的项目文件夹")
        # 先登记去向再移动：中断后据此认出目标文件夹属于本批次。批次的 _回收区 不在项目文件夹内，不随之移动。
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            batch.payload = {**batch.payload, "ingest_target": target}
        fs.rename(source, target)

    def _assign_codes(self, batch_id: int, fs: NasFs) -> None:
        model = self.ai_settings.effective().model_vision or ("fake-vision" if self.auth.config.mode == "dev" else "")
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            files = {row.id: row for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if not row.removed}
            rows = [row for row in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id)) if row.batch_file_id in files]
            if "ingested_at" not in batch.payload:
                # 入库时间和模型名只定一次，断点继续时档案内容不随日期变化。
                batch.payload = {**batch.payload, "ingested_at": utcnow().isoformat(), "ingest_model": model}
            if all(row.product_code for row in rows):
                return
            if len(rows) > 999:
                raise IngestError("商品超过 999 个，三位流水不够用")
            # 套装和单品共用一套流水：先单品、后套装，各自按文件名排序。
            ordered = sorted(rows, key=lambda row: (row.kind != "item", files[row.batch_file_id].rel_path.rsplit("/", 1)[-1]))
            for index, row in enumerate(ordered, 1):
                row.product_code = f"{batch.project_code}-{index:03d}"

    def _archive(self, batch_id: int) -> tuple[str, dict[str, str]]:
        with self.auth.lock, self.auth.database.session() as session:
            batch, _, _, target = self._facts(session, batch_id)
            return target, archive.documents(archive.from_batch(session, batch))

    def _write_product_archives(self, batch_id: int, fs: NasFs) -> None:
        target, texts = self._archive(batch_id)
        for directory in archive.ARCHIVE_DIRS.values():
            if not fs.exists(f"{target}/{directory}"):
                fs.mkdir(f"{target}/{directory}")
        archive.write_documents(fs, target, {path: text for path, text in texts.items() if path != archive.PROJECT_ARCHIVE})

    def _write_project_archive(self, batch_id: int, fs: NasFs) -> None:
        target, texts = self._archive(batch_id)
        if not fs.exists(f"{target}/{archive.HISTORY_DIR}"):
            fs.mkdir(f"{target}/{archive.HISTORY_DIR}")
        archive.write_documents(fs, target, {archive.PROJECT_ARCHIVE: texts[archive.PROJECT_ARCHIVE]})

    def _register(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch, _, _, target = self._facts(session, batch_id)
            project = session.get(Project, batch.project_id) if batch.project_id else None
            if project is not None and project.status == "active":
                return
            listing = [(row.id, row.rel_path, row.size) for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id)) if not row.removed]
        # 登记前逐个核对 NAS 上的实际文件，卡片柜只收录确实在正式目录里的文件。
        times: dict[int, int] = {}
        for file_id, rel_path, size in listing:
            try:
                stat = fs.stat(f"{target}/{rel_path}")
            except NasNotFound:
                raise IngestError(f"正式目录缺少文件：{rel_path}") from None
            if stat.size != size:
                raise IngestError(f"文件大小与上传记录不一致：{rel_path}")
            times[file_id] = int(stat.mtime * 1000)
        with self.auth.lock, self.auth.database.session() as session:
            # 以下全部在同一个事务里：要么整套登记完成，要么一条都不留。
            batch, folder, _, _ = self._facts(session, batch_id)
            parts = folder.split("_")
            moment = datetime.fromisoformat(str(batch.payload["ingested_at"]))
            segments = str(batch.final_path).split("/")
            industry = session.scalar(select(Industry).where(Industry.name == segments[1][2:])) if len(segments) == 3 else None
            company = next((row for row in session.scalars(select(CompanyIndustryMap)) if len(segments) == 3
                            and f"{row.company_seq:02d}{row.company_name}" == segments[2] and row.industry_name == segments[1][2:]), None)
            project = Project(project_code=batch.project_code, code_source="system_h" if batch.project_code.startswith("H") else "company",
                company_name=parts[1], company_id=company.id if company else None, industry_id=industry.id if industry else None,
                won=batch.won, project_name=parts[2], folder_name=folder, current_path=f"{batch.final_path}/{folder}", status="active",
                ingested_at=moment, archive_updated_at=moment)
            session.add(project)
            session.flush()
            kinds = batch.payload.get("fingerprint_kinds", {})
            catalog: dict[int, CatalogFile] = {}
            for row in session.scalars(select(BatchFile).where(BatchFile.batch_id == batch.id).order_by(BatchFile.rel_path)):
                if row.removed:
                    continue
                catalog[row.id] = CatalogFile(project_id=project.id, rel_path=row.rel_path, size=row.size, mtime=times[row.id],
                    fingerprint=row.fingerprint or "", fingerprint_kind=str(kinds.get(str(row.id), "full")), category=category_of(row.rel_path),
                    status="active", first_batch_id=batch.id)
                session.add(catalog[row.id])
            session.flush()
            docs = {doc.code: doc for doc in archive.from_batch(session, batch).products}
            products: dict[int, Product] = {}
            rows = [row for row in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch.id).order_by(BatchProduct.product_code))
                    if row.batch_file_id in catalog]
            for row in rows:
                products[row.id] = Product(product_code=row.product_code, project_id=project.id, kind=row.kind, display_name=row.display_name,
                    file_id=catalog[row.batch_file_id].id, archive_rel_path=docs[row.product_code].archive_rel_path, status="active",
                    free_text=row.free_text, vision_model=str(batch.payload.get("ingest_model", "")), ingested_at=moment)
                session.add(products[row.id])
            session.flush()
            for row in rows:
                for key, tag in row.tags.items():
                    if isinstance(tag, dict):
                        session.add(ProductTag(product_id=products[row.id].id, field_key=key, value=str(tag.get("value", "")),
                            terms=[str(term) for term in tag.get("terms") or []], source=str(tag.get("source", "ai")),
                            ai_value=str(tag.get("ai_value", "")), flag=tag.get("flag") if isinstance(tag.get("flag"), str) else None))
                if row.kind == "set":
                    for item in row.relation_items:
                        if isinstance(item, dict) and item.get("batch_product_id") in products:
                            session.add(ProductRelation(set_product_id=products[row.id].id, item_product_id=products[item["batch_product_id"]].id,
                                source=str(item.get("source", "ai")), confirmed_by=batch.owner_id, confirmed_at=moment))
            batch.project_id = project.id
            OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "登记卡片柜", "project", str(project.id), None,
                {"project_code": project.project_code, "current_path": project.current_path, "files": len(catalog), "products": len(products)}, "成功", "入库任务")

    def _links(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            ensure_link(session, "project", batch.project_id)
            for product_id in session.scalars(select(Product.id).where(Product.project_id == batch.project_id)):
                ensure_link(session, "product", product_id)
            for file_id in session.scalars(select(CatalogFile.id).where(CatalogFile.project_id == batch.project_id)):
                ensure_link(session, "file", file_id)

    def _finish(self, batch_id: int, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            if batch.status == DONE:
                return
            pairs = [(row.batch_file_id, product_id) for row, product_id in session.execute(select(BatchProduct, Product.id).join(
                Product, Product.product_code == BatchProduct.product_code).where(BatchProduct.batch_id == batch.id))]
        # 缩略图缓存从批次目录迁到商品目录；重复执行时已迁移的跳过。
        source_root = self.thumbs / "batch" / str(batch_id)
        for file_id, product_id in pairs:
            source, destination = source_root / str(file_id), self.thumbs / "product" / str(product_id)
            if source.is_dir() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
        if source_root.is_dir():
            shutil.rmtree(source_root, ignore_errors=True)
        with self.auth.lock, self.auth.database.session() as session:
            batch = session.get(Batch, batch_id)
            batch.status, batch.ingest_error = DONE, None
            for _, product_id in pairs:
                self.queue.enqueue("embed", {"product_id": product_id}, session=session)
            OplogService(session).write(batch.final_reviewer_id, "final_reviewer", "入库完成", "batch", str(batch.id),
                                       {"status": INGESTING}, {"status": DONE, "project_id": batch.project_id}, "成功", "入库任务")
            # 通知上传人、初审、终审。
            self.notify.emit(session, "ingested", batch)

    @staticmethod
    def product_thumb(data_dir: Path, product_id: int, size: int) -> Path:
        return data_dir / "thumbs" / "product" / str(product_id) / f"{size}.jpg"
