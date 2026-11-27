"""按审核快照执行入库后操作，只有 SQLite 提交后新版本才公开。"""
from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Batch, ExpectedDeletion, InspectionFinding, Job, PostopPlan, Project, ReviewRound, ReviewSnapshot, User, UserSession, utcnow
from app.errors import AppError
from app.schemas.audit import CHECKLIST
from app.schemas.postop_execution import DeleteFileView, OperationExecution
from app.services import archive
from app.services.archive import publication
from app.services.archive import repair
from app.services.ai.orchestrator import AiService
from app.services.auth.service import AuthService, LoginContext
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.nas import NasFs, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.notify import NotifyService
from app.services.oplog import OplogService
from app.services.review import ReviewService
from app.services.search.indexes import IndexService
from app.services.upload.service import UploadService

from . import embeddings, manifest
from .shadow import VERSIONS, WORKSPACE, ShadowTree, verify_file

RUNNING, ERROR, DONE = "入库中", "入库异常", "已入库"
STEPS = ("校验审核和资产锁", "预备文件与档案", "准备正式及候选向量", "核验版本目录", "核对物理删除清单", "发布")


class PostopExecutor:
    def __init__(self, auth: AuthService, queue: JobQueue, ai: AiService, notify: NotifyService, indexes: IndexService) -> None:
        self.after_publication: Callable[[Session], None] | None = None
        self.auth, self.queue, self.ai, self.notify, self.indexes = auth, queue, ai, notify, indexes
        self.paths = create_paths(auth.config)

    @staticmethod
    def _batch(session: Session, batch_id: int) -> Batch:
        batch = session.get(Batch, batch_id)
        if batch is None or batch.kind == "ingest":
            raise AppError("operation_not_found", "入库后操作批次不存在", 404)
        return batch

    @staticmethod
    def _snapshot(session: Session, batch: Batch) -> ReviewSnapshot:
        if not batch.lock_active or batch.status not in (RUNNING, ERROR):
            raise AppError("invalid_batch_state", "操作批次状态或项目锁已失效", 409)
        snapshot = ReviewService.submitted_snapshot(session, batch)
        rounds = {row.stage: row for row in session.scalars(select(ReviewRound).where(
            ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round, ReviewRound.snapshot_id == snapshot.id,
            ReviewRound.decision == "pass"))}
        first, final = rounds.get("first"), rounds.get("final")
        if (first is None or final is None or first.reviewer_id != batch.first_reviewer_id
                or final.reviewer_id != batch.final_reviewer_id or first.reviewer_id == final.reviewer_id
                or not final.execution_session_id
                or any(row.checklist != dict.fromkeys(CHECKLIST, True) for row in (first, final))
                or ReviewService._digest(snapshot.content) != snapshot.digest
                or snapshot.content.get("operation") is None):
            raise AppError("operation_review_invalid", "双审或审核快照校验失败", 409)
        return snapshot

    @staticmethod
    def _log(session: Session, batch: Batch, action: str, before: object, after: object,
             host: str = "入库后任务", result: str = "成功") -> None:
        OplogService(session).write(batch.final_reviewer_id, "final_reviewer", action, "batch", str(batch.id), before,
                                    after, result, host)

    def enqueue(self, session: Session, batch: Batch, user_id: int, login_id: int) -> None:
        job = self.queue.enqueue("postop", {"batch_id": batch.id}, user_id, session=session, run_as_session_id=login_id)
        batch.payload = {**batch.payload, "postop_job_id": job.id}

    def _progress(self, session: Session, batch: Batch, user: User) -> OperationExecution:
        final = user.id == batch.final_reviewer_id and "final_reviewer" in user.roles
        if not (user.id in (batch.owner_id, batch.uploader_id) or final or "admin" in user.roles
                or user.id == batch.first_reviewer_id and "first_reviewer" in user.roles):
            raise AppError("forbidden", "无权查看操作进度", 403)
        active = batch.payload.get("postop_job_id")
        job = session.get(Job, active) if active else None
        can_resume = final and batch.status in (RUNNING, ERROR) and (job is None or job.status in ("failed", "done"))
        response = OperationExecution(status=batch.status, step=batch.ingest_step,
            step_name="已发布" if batch.status == DONE else STEPS[min(batch.ingest_step, len(STEPS) - 1)],
            error=batch.ingest_error, can_resume=can_resume)
        if batch.kind == "purge" and batch.review_snapshot_hash:
            operation = ReviewService.submitted_snapshot(session, batch).content["operation"]
            purge = operation["purge"]
            response.confirmation_code = operation["params"]["confirm"] if can_resume else None
            response.purge_files = [DeleteFileView(file_id=row["file_id"], path=row["path"], size=row["size"],
                fingerprint=row["fingerprint"], history=row.get("historical", False)) for row in purge["files"]]
            response.product_codes = [purge["project_code"], *(row["product_code"] for row in purge["products"])]
            response.relationships = [f'{row["set_product_id"]} → {row["item_product_id"]}' for row in purge["relations"]]
        return response

    def read(self, batch_id: int, login: LoginContext) -> OperationExecution:
        with self.auth.database.session() as session:
            UploadService._check_login(session, login)
            user = session.get(User, login.user.id)
            if user is None or not user.enabled:
                raise AppError("forbidden", "当前账号没有此操作权限", 403)
            return self._progress(session, self._batch(session, batch_id), user)

    def resume(self, batch_id: int, confirm: str | None, login: LoginContext, host: str) -> OperationExecution:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            batch = self._batch(session, batch_id)
            user = session.get(User, login.user.id)
            if not user or not user.enabled or user.id != batch.final_reviewer_id or "final_reviewer" not in user.roles:
                raise AppError("forbidden", "仅本轮终审本人可以执行或继续操作", 403)
            frozen = self._snapshot(session, batch)
            self.auth.pool.get(login.session_id)
            if batch.kind == "purge" and (not confirm or confirm != frozen.content["operation"]["params"]["confirm"]):
                raise AppError("purge_confirmation_required", "请输入审核清单中的指定编号", 422)
            active = batch.payload.get("postop_job_id")
            job = session.get(Job, active) if active else None
            if job is None or job.status not in ("running", "queued"):
                self._log(session, batch, "确认物理删除并继续" if batch.kind == "purge" else "继续入库后操作",
                          {"status": batch.status}, {"snapshot": frozen.digest, "step": batch.ingest_step,
                                                   "execution_session_id": login.session_id}, host)
                self.enqueue(session, batch, user.id, login.session_id)
                batch.status, batch.ingest_error = RUNNING, None
            return self._progress(session, batch, user)

    def _actor_batch(self, session: Session, context: JobContext) -> Batch:
        job = context.job
        batch = self._batch(session, job.payload["batch_id"])
        if batch.status != RUNNING or batch.payload.get("postop_job_id") != job.id or job.run_as_user_id != batch.final_reviewer_id:
            raise JobFailure("操作任务已失效或执行人不是本轮终审", retryable=False)
        user = session.get(User, job.run_as_user_id)
        login = session.get(UserSession, job.run_as_session_id)
        if not user or not user.enabled or "final_reviewer" not in user.roles or not login or login.user_id != user.id:
            raise JobFailure("终审资格或设备会话已失效", retryable=False)
        UploadService._check_login(session, LoginContext(user, login.id))
        if self.auth.pool.get(login.id) is not context.fs:
            raise JobFailure("终审 NAS 会话已变更，请重新登录后继续", retryable=False)
        return batch

    def _for_job(self, session: Session, context: JobContext) -> tuple[Batch, ReviewSnapshot]:
        batch = self._actor_batch(session, context)
        return batch, self._snapshot(session, batch)

    @contextmanager
    def _guard(self, context: JobContext) -> Iterator[None]:
        with self.auth.lock:
            with self.auth.database.session() as session:
                self._actor_batch(session, context)
            yield

    def _checkpoint(self, session: Session, batch: Batch, step: int) -> None:
        if batch.ingest_step < step:
            self._log(session, batch, "入库后操作步骤完成", {"step": batch.ingest_step},
                      {"step": step, "name": STEPS[step - 1]})
            batch.ingest_step = step

    def _target(self, session: Session, fs: NasFs, batch: Batch, project: Project, canonical: str) -> str:
        prior = set(session.scalars(select(PostopPlan.previous_path).join(Batch, Batch.id == PostopPlan.batch_id)
                                   .where(Batch.project_id == project.id, Batch.status == DONE)))
        prior.add(project.current_path)
        parent, _, folder = canonical.rpartition("/")
        destination = self.paths.join(Region.FORMAL, canonical)
        occupied = fs.exists(destination)
        parent_root = self.paths.join(Region.FORMAL, parent) if parent else self.paths.root(Region.FORMAL)
        if not occupied and fs.exists(parent_root):
            occupied = any(name.casefold() == folder.casefold() for name in fs.list(parent_root))
        if occupied:
            known = next((path for path in prior if path.casefold() == canonical.casefold()), None)
            if not known:
                raise JobFailure("审核目标目录已被其他资产占用，请人工核验后继续", retryable=False)
            proof = archive.parse(fs, self.paths.join(Region.FORMAL, known))
            if proof["project"]["id"] != project.id:
                raise JobFailure("原版本目录的实际归属已变化，请人工核验后继续", retryable=False)
        if session.scalar(select(Project.id).where(Project.id != project.id,
                          func.lower(Project.current_path) == canonical.lower())) is not None:
            raise JobFailure("审核目标与另一项目的公开目录冲突", retryable=False)
        if occupied:
            version_dir = safe_join(parent, VERSIONS) if parent else VERSIONS
            return safe_join(version_dir, batch.batch_no, folder)
        return canonical

    def _plan(self, context: JobContext, fs: NasFs) -> PostopPlan:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, frozen = self._for_job(session, context)
            prior = session.get(PostopPlan, batch.id)
            if prior is not None:
                return prior
            project = session.get(Project, batch.project_id)
            if not project:
                raise JobFailure("原项目不存在", retryable=False)
            canonical = manifest.logical_path(session, batch, frozen)
            target = self._target(session, fs, batch, project, canonical)
            result = manifest.plan(session, batch, frozen, target, canonical_path=canonical)
            temp_dirs = []
            if batch.kind == "supplement":
                # 空目录也属于已提交的上载清单。
                temp_dirs = list(frozen.content.get("directories", []))
            record = PostopPlan(batch_id=batch.id, previous_path=project.current_path,
                target_path=target, baseline=result["baseline"], manifest=result["manifest"], sources=result["sources"],
                purge=result["purge"], progress={"nonce": secrets.token_hex(32), "upload_directories": temp_dirs,
                                             "batch_product_codes": result["batch_product_codes"],
                                             "base_hash": result["old_manifest_hash"]})
            session.add(record)
            self._checkpoint(session, batch, 1)
            return record

    def _advance(self, context: JobContext, step: int, **progress: object) -> PostopPlan:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, _ = self._for_job(session, context)
            plan = session.get(PostopPlan, batch.id)
            if progress:
                plan.progress = {**plan.progress, **progress}
            self._checkpoint(session, batch, step)
            return plan

    def _purge_scope(self, session: Session, batch: Batch, frozen: ReviewSnapshot, plan: PostopPlan) -> None:
        project = session.get(Project, batch.project_id)
        operation = frozen.content["operation"]
        preview = manifest.purge_preview(session, project, operation["params"])
        if (not preview["eligible"] or preview["files"] != operation["purge"]["files"]
                or preview["files"] != plan.purge or manifest.digest(archive.from_catalog(session, project)) != plan.progress["base_hash"]):
            raise JobFailure("物理删除范围、冷静期或原版本已变化", retryable=False)

    def _purge(self, context: JobContext, fs: NasFs, plan: PostopPlan) -> None:
        if not plan.purge:
            return
        with self.auth.database.session() as session:
            batch, frozen = self._for_job(session, context)
            self._purge_scope(session, batch, frozen, plan)
            recorded = {row.path: row for row in session.scalars(select(ExpectedDeletion).where(ExpectedDeletion.batch_id == batch.id))}
        # 先核对全部对象，再登记意图；陌生或变化的对象阻断本轮删除。
        for row in plan.purge:
            path = self.paths.join(Region.FORMAL, row["path"])
            old = recorded.get(row["path"])
            if old and (old.file_id, old.fingerprint, old.size) != (row["file_id"], row["fingerprint"], row["size"]):
                raise JobFailure("预期删除记录与审核清单不一致", retryable=False)
            held = self._quarantine(plan, old) if old else None
            if fs.exists(path):
                if old and (old.quarantined_at or (held and fs.exists(held))):
                    raise JobFailure("已隔离位置出现新文件，已停止续跑", retryable=False)
                verify_file(fs, path, row)
            elif held and fs.exists(held):
                verify_file(fs, held, row)
            elif not old or not old.quarantined_at:
                raise JobFailure("审核文件在受控隔离前已缺失，请核查实际位置", retryable=False)
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, frozen = self._for_job(session, context)
            self._purge_scope(session, batch, frozen, plan)
            for row in plan.purge:
                if row["path"] not in recorded:
                    record = ExpectedDeletion(batch_id=batch.id, path=row["path"], file_id=row["file_id"],
                                              size=row["size"], fingerprint=row["fingerprint"])
                    session.add(record)
                    recorded[row["path"]] = record
            if any(record.id is None for record in recorded.values()):
                self._log(session, batch, "登记预期物理删除", None, plan.purge)
        for row in plan.purge:
            self._delete_one(context, fs, plan, recorded[row["path"]], row)

    def _quarantine(self, plan: PostopPlan, record: ExpectedDeletion) -> str:
        return self.paths.join(Region.FORMAL, WORKSPACE, "删除-" + plan.progress["nonce"], str(record.id))

    def _delete_one(self, context: JobContext, fs: NasFs, plan: PostopPlan, record: ExpectedDeletion, row: dict) -> None:
        source = self.paths.join(Region.FORMAL, row["path"])
        held = self._quarantine(plan, record)
        if record.completed_at:
            if fs.exists(source) or fs.exists(held):
                raise JobFailure("已删除位置出现文件，已停止续跑", retryable=False)
            return
        with self._guard(context):
            if not record.quarantined_at and fs.exists(source):
                verify_file(fs, source, row)
                archive.mkdir(fs, held.rpartition("/")[0], recursive=True)
                fs.rename(source, held)
            if fs.exists(source):
                raise JobFailure("隔离后原位置被重新占用，已停止删除", retryable=False)
            if fs.exists(held):
                verify_file(fs, held, row)
            elif not record.quarantined_at:
                raise JobFailure("无法核对隔离后的文件，已停止删除", retryable=False)
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, frozen = self._for_job(session, context)
            self._purge_scope(session, batch, frozen, plan)
            current = session.get(ExpectedDeletion, record.id)
            if current.quarantined_at is None:
                current.quarantined_at = utcnow()
                self._log(session, batch, "隔离已审核删除文件", row, {"file_id": row["file_id"], "intent": current.id})
        with self._guard(context):
            if fs.exists(source):
                raise JobFailure("原位置被重新占用，已停止删除", retryable=False)
            if fs.exists(held):
                verify_file(fs, held, row)
                fs.delete(held)
            if fs.exists(held):
                raise JobFailure("文件删除后仍可见，请检查 NAS")
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, _ = self._for_job(session, context)
            current = session.get(ExpectedDeletion, record.id)
            if current.completed_at is None:
                current.completed_at = utcnow()
                self._log(session, batch, "完成预期文件删除", row, {"file_id": row["file_id"]})

    def _publish(self, context: JobContext, tree: ShadowTree, plan: PostopPlan) -> None:
        tree.verify(tree.target)
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, _ = self._for_job(session, context)
            if plan.purge:
                records = {row.path for row in session.scalars(select(ExpectedDeletion).where(
                    ExpectedDeletion.batch_id == batch.id, ExpectedDeletion.completed_at.is_not(None)))}
                if records != {row["path"] for row in plan.purge}:
                    raise JobFailure("预期删除清单尚未完成")
            result = {"manifest": plan.manifest, "sources": plan.sources, "purge": plan.purge, "baseline": plan.baseline,
                      "old_manifest_hash": plan.progress["base_hash"], "old_root": plan.previous_path,
                      "project_status": plan.manifest["project"]["status"],
                      "batch_product_codes": plan.progress["batch_product_codes"]}
            manifest.publish(session, batch, result)
            session.flush()
            project = session.get(Project, batch.project_id)
            embeddings.publish(self.ai, session, plan.manifest, batch.id, plan.progress["vectors"])
            current_plan = session.get(PostopPlan, batch.id)
            current_plan.progress = {**current_plan.progress, "published_at": utcnow().isoformat()}
            batch.status, batch.ingest_error = DONE, None
            self._checkpoint(session, batch, len(STEPS))
            self._log(session, batch, "发布入库后操作", {"path": plan.previous_path},
                      {"path": plan.target_path, "version": project.asset_version, "kind": batch.kind})
            self.notify.emit(session, "ingested", batch)
            self.indexes.sync_after_publish(session)
            if self.after_publication is not None:
                self.after_publication(session)
            publication.seal(session, batch, tree.fs, tree.target, plan.manifest)

    def on_job_state(self, session: Session, job: Job) -> None:
        if job.status != "failed":
            return
        batch = session.get(Batch, job.payload["batch_id"])
        if batch and batch.status == RUNNING and batch.payload.get("postop_job_id") == job.id:
            batch.status, batch.ingest_error = ERROR, job.error
            self._log(session, batch, "入库后操作异常", {"status": RUNNING},
                      {"step": batch.ingest_step, "error": job.error}, result="失败")
            self.notify.emit(session, "ingest_error", batch)

    def _repair(self, context: JobContext, fs: NasFs) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            batch, snapshot = self._for_job(session, context)
            project = session.get(Project, batch.project_id)
            operation = snapshot.content["operation"]
            if project is None:
                raise JobFailure("待修复项目不存在", retryable=False)
            data = archive.from_catalog(session, project)
            if manifest.digest(data) != operation["before_digest"]:
                raise JobFailure("正式档案依据已变化，无法修复", retryable=False)
            root = self.paths.join(Region.FORMAL, project.current_path)
            repair.check(fs, root, data, operation["repair"])
            archive.rebuild(session, project, fs, root)
            if archive.verify(session, project, fs, root):
                raise JobFailure("修复后档案仍与卡片柜不一致", retryable=False)
            batch.status, batch.ingest_error, batch.ingest_step = DONE, None, len(STEPS)
            for row in session.scalars(select(InspectionFinding).where(InspectionFinding.project_id == project.id,
                    InspectionFinding.kind == "archive_broken", InspectionFinding.resolved_at.is_(None))):
                row.resolved_at = utcnow()
            self._log(session, batch, "档案修复并核验", None, {"project_id": project.id, "paths": list(operation["repair"])})
            self.notify.emit(session, "ingested", batch)
            publication.seal(session, batch, fs, root, data)

    def handle(self, context: JobContext) -> None:
        if context.fs is None:
            raise JobFailure("缺少终审本人 NAS 会话", retryable=False)
        try:
            with self.auth.database.session() as session:
                batch = self._actor_batch(session, context)
            if batch.kind == "archive_repair":
                self._repair(context, context.fs)
                return
            plan = self._plan(context, context.fs)
            with self.auth.database.session() as session:
                batch, _ = self._for_job(session, context)
            tree = ShadowTree(context.fs, self.paths, plan, batch.batch_no, lambda: self._guard(context))
            tree.prepare()
            self._advance(context, 2)
            staged_vectors = embeddings.prepare(self.ai, plan.manifest, batch.id, plan.progress.get("vectors", {}))
            plan = self._advance(context, 3, vectors=staged_vectors)
            tree.install()
            self._advance(context, 4)
            self._purge(context, context.fs, plan)
            self._advance(context, 5)
            self._publish(context, tree, plan)
        except AppError as exc:
            raise JobFailure(exc.message, retryable=False) from None
        except archive.ArchiveError as exc:
            raise JobFailure(str(exc), retryable=False) from None
