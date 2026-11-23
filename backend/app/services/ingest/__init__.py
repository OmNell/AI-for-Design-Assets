"""以当前轮终审会话幂等执行正式入库，全部必要向量就绪后发布。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (Batch, BatchProduct, CatalogFile, CompanyIndustryMap, Industry, IngestPlan, Job, Product, ProductEmbedding,
                           Project, ReviewRound, ReviewSnapshot, Setting, User, UserSession, utcnow)
from app.errors import AppError
from app.schemas.audit import CHECKLIST
from app.schemas.ingest import IngestProgress
from app.services import archive
from app.services.archive import publication
from app.services.admin.company_map import WON_FOLDERS
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.ai.orchestrator import AiService
from app.services.ai.vision import matches_frozen_description
from app.services.auth.service import AuthService, LoginContext
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.nas import NasFs, NasPermissionDenied, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.notify import NotifyService
from app.services.oplog import OplogService
from app.services.preflight import REQUIRED
from app.services.review import ReviewService
from app.services.review.policy import self_review_allowed
from app.services.upload.fingerprint import fingerprint
from app.services.upload.service import UploadService
from . import vectors

INGESTING, ERROR, DONE = "入库中", "入库异常", "已入库"
STEPS = ("校验审核版本、指纹和目标", "建立正式目录层级", "移动项目并补齐资料夹", "预分配编号和永久身份",
         "写入商品档案", "写入项目档案", "登记卡片柜", "校验永久链接与档案", "生成向量并发布")
MARKER = "_工作台入库校验"
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Execution:
    batch: Batch
    snapshot: ReviewSnapshot
    final_review: ReviewRound
    source: str
    target: str
    folder: str
    relative: str


class IngestService:
    def __init__(self, auth: AuthService, queue: JobQueue, ai: AiService, notify: NotifyService) -> None:
        self.auth, self.queue, self.ai, self.notify = auth, queue, ai, notify
        self.paths = create_paths(auth.config)
        self.on_published: Callable[[Session], None] | None = None

    def enqueue(self, session: Session, batch: Batch, reviewer_id: int, login_id: int) -> None:
        job = self.queue.enqueue("ingest", {"batch_id": batch.id}, reviewer_id, session=session, run_as_session_id=login_id)
        batch.payload = {**batch.payload, "ingest_job_id": job.id,
                         "ingest_move_id": batch.payload.get("ingest_move_id") or secrets.token_hex(32)}

    def _batch(self, session: Session, batch_id: int) -> Batch:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise AppError("batch_not_found", "批次不存在", 404)
        return batch

    def read(self, batch_id: int, login: LoginContext) -> IngestProgress | None:
        with self.auth.lock, self.auth.database.session() as session:
            UploadService._check_login(session, login)
            batch = self._batch(session, batch_id)
            return self.progress(session, batch, session.get(User, login.user.id))

    def progress(self, session: Session, batch: Batch, user: User) -> IngestProgress | None:
        if batch.kind != "ingest" or batch.status not in (INGESTING, ERROR, DONE):
            return None
        allowed = (user.id in (batch.owner_id, batch.uploader_id)
                   or user.id == batch.first_reviewer_id and "first_reviewer" in user.roles
                   or user.id == batch.final_reviewer_id and "final_reviewer" in user.roles
                   or "admin" in user.roles)
        frozen = ReviewService.submitted_snapshot(session, batch)
        if not allowed and user.id != frozen.submitted_by:
            raise AppError("forbidden", "无权查看此批次入库进度", 403)
        ready = 0
        plan = session.get(IngestPlan, batch.id)
        if plan and batch.project_id:
            try:
                model = vectors.model_for(self.ai, session)
                ids = [row["id"] for row in plan.manifest["products"]]
                ready = sum(1 for pid in ids if (row := session.get(ProductEmbedding, model.key(pid))) and row.state == "ready")
            except JobFailure:
                pass
        products = len(frozen.content["review"]["products"])
        done = batch.status == DONE
        index = min(batch.ingest_step, len(STEPS) - 1)
        return IngestProgress(status=batch.status, step=batch.ingest_step, step_name="入库完成" if done else STEPS[index],
            error=batch.ingest_error, products=products, files=len(frozen.content["files"]), vectors_ready=products if done else ready,
            can_resume=(batch.status == ERROR or (batch.status == INGESTING and not batch.payload.get("ingest_job_id")))
            and user.id == batch.final_reviewer_id and "final_reviewer" in user.roles)

    def resume(self, batch_id: int, login: LoginContext, host: str) -> IngestProgress:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            batch = self._batch(session, batch_id)
            user = session.get(User, login.user.id)
            if not user.enabled or "final_reviewer" not in user.roles or user.id != batch.final_reviewer_id:
                raise AppError("forbidden", "仅本轮终审本人可以继续入库", 403)
            if batch.kind != "ingest" or batch.status not in (INGESTING, ERROR):
                raise AppError("invalid_batch_state", "该批次当前不能继续入库", 409)
            self.auth.pool.get(login.session_id)
            try:
                self._execution(session, batch)
            except JobFailure as exc:
                raise AppError("invalid_ingest", exc.message, 409) from None
            active = session.get(Job, batch.payload.get("ingest_job_id")) if batch.payload.get("ingest_job_id") else None
            if active is None or active.status not in ("queued", "running"):
                before = batch.status
                self.enqueue(session, batch, user.id, login.session_id)
                batch.status, batch.ingest_error = INGESTING, None
                project = session.get(Project, batch.project_id) if batch.project_id else None
                if project is not None:
                    project.status = "ingesting"
                self._log(session, batch, "继续入库", {"status": before}, {"status": INGESTING}, host)
            return self.progress(session, batch, user)

    def _execution(self, session: Session, batch: Batch) -> Execution:
        if batch.kind != "ingest" or not batch.lock_active or not batch.final_path or batch.status not in (INGESTING, ERROR):
            raise JobFailure("批次状态或项目独占锁已失效", retryable=False)
        snapshot = ReviewService.submitted_snapshot(session, batch)
        rows = {row.stage: row for row in session.scalars(select(ReviewRound).where(
            ReviewRound.snapshot_id == snapshot.id, ReviewRound.batch_id == batch.id, ReviewRound.round == batch.round,
            ReviewRound.decision == "pass"))}
        first, final = rows.get("first"), rows.get("final")
        submitters = set(session.scalars(select(ReviewSnapshot.submitted_by).where(ReviewSnapshot.batch_id == batch.id)))
        submitters.add(batch.uploader_id)
        if (not first or not final or first.reviewer_id != batch.first_reviewer_id or final.reviewer_id != batch.final_reviewer_id
                or any(row.checklist != dict.fromkeys(CHECKLIST, True) for row in (first, final))
                or ReviewService._digest(snapshot.content) != snapshot.digest):
            raise JobFailure("本轮双审、回避或快照校验失败", retryable=False)
        reviewers = {first.reviewer_id, final.reviewer_id}
        conflicts = reviewers if len(reviewers) == 1 else reviewers & submitters
        if any(not self_review_allowed(session, self.auth.config.review, batch, session.get(User, reviewer))
               for reviewer in conflicts):
            raise JobFailure("本轮审核回避校验失败", retryable=False)
        original = snapshot.content["review"]["batch"]["folder_name"]
        code, company, name = original.split("_")
        location = (final.path_change or {}).get("after", {})
        segments = batch.final_path.split("/")
        if (code != batch.project_code or len(segments) != 3 or location.get("path") != batch.final_path
                or segments[0] != WON_FOLDERS.get(location.get("won"))
                or any(not re.fullmatch(r"[0-9]{2}.+", segment) for segment in segments[1:])):
            raise JobFailure("快照项目编号或终审核定目录不一致", retryable=False)
        if "before" in (final.path_change or {}):
            company = segments[2][2:]
        folder = f"{code}_{company}_{name}"
        if len(folder.split("_")) != 3 or "/" in folder or "\\" in folder:
            raise JobFailure("终审后的项目文件夹名不合法", retryable=False)
        relative = safe_join(batch.final_path, folder)
        return Execution(batch, snapshot, final, self.paths.join(Region.TEMP, batch.batch_no, original),
                         self.paths.join(Region.FORMAL, relative), folder, relative)

    def _for_job(self, session: Session, context: JobContext) -> Execution:
        job = context.job
        batch = self._batch(session, job.payload["batch_id"])
        if batch.payload.get("ingest_job_id") != job.id or batch.status != INGESTING or job.run_as_user_id != batch.final_reviewer_id:
            raise JobFailure("该任务已失效或执行人不是本轮终审", retryable=False)
        user = session.get(User, job.run_as_user_id)
        login = session.get(UserSession, job.run_as_session_id)
        if not user or not user.enabled or "final_reviewer" not in user.roles or not login or login.user_id != user.id:
            raise JobFailure("终审资格或设备会话已失效", retryable=False)
        UploadService._check_login(session, LoginContext(user, login.id))
        return self._execution(session, batch)

    def _log(self, session: Session, batch: Batch, action: str, before: object, after: object,
             host: str = "入库任务") -> None:
        OplogService(session).write(batch.final_reviewer_id, "final_reviewer", action, "batch", str(batch.id),
                                    before, after, "失败" if action == "入库异常" else "成功", host)

    def on_job_state(self, session: Session, job: Job) -> None:
        if job.status != "failed":
            return
        batch = session.get(Batch, job.payload["batch_id"])
        if batch is None or batch.status != INGESTING or batch.payload.get("ingest_job_id") != job.id:
            return
        batch.status, batch.ingest_error = ERROR, job.error
        if batch.project_id:
            session.get(Project, batch.project_id).status = "ingest_error"
        self._log(session, batch, "入库异常", {"status": INGESTING}, {"step": batch.ingest_step + 1, "error": job.error})
        self.notify.emit(session, "ingest_error", batch)

    @staticmethod
    def _files(fs: NasFs, root: str, skip: set[str], prefix: str = "") -> Iterator[tuple[str, bool]]:
        for name in fs.list(safe_join(root, prefix) if prefix else root):
            path = safe_join(prefix, name) if prefix else safe_join(name)
            if path in skip:
                continue
            is_dir = fs.stat(safe_join(root, path)).is_dir
            yield path, is_dir
            if is_dir:
                yield from IngestService._files(fs, root, skip, path)

    def _verify_files(self, fs: NasFs, execution: Execution, root: str, *, after_rename: bool) -> None:
        snap = execution.snapshot.content
        expected = {row["rel_path"]: row for row in snap["files"]}
        dirs = set(snap["directories"])
        for path in list(expected) + list(dirs):
            parts = path.split("/")
            dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
        skip = {MARKER, MARKER + ".tmp"}
        if after_rename and execution.batch.ingest_step >= 4:
            skip.update([*archive.PRODUCT_DIR.values(), archive.HISTORY_DIR, archive.PROJECT_FILE])
        scanned = dict(self._files(fs, root, skip))
        files = {path for path, is_dir in scanned.items() if not is_dir}
        actual_dirs = {path for path, is_dir in scanned.items() if is_dir}
        if files != set(expected) or not dirs.issubset(actual_dirs) or actual_dirs - dirs - (set(REQUIRED) if after_rename else set()):
            raise JobFailure("NAS 项目清单与本轮冻结清单不一致", retryable=False)
        for rel, row in expected.items():
            if row["fingerprint_kind"] != "full" or not SHA256.fullmatch(row["fingerprint"] or ""):
                raise JobFailure("冻结清单缺少完整 SHA-256", retryable=False)
            path = safe_join(root, rel)
            if fs.stat(path).size != row["size"] or fingerprint(fs, path, row["size"]) != row["fingerprint"]:
                raise JobFailure("NAS 文件与审核时的完整 SHA-256 不一致", retryable=False)

    @staticmethod
    def _marker(fs: NasFs, root: str, token: str) -> bool:
        path = safe_join(root, MARKER)
        if not fs.exists(path):
            return False
        with fs.open_read(path) as stream:
            return stream.read(65) == token.encode("ascii")

    def _check(self, fs: NasFs, execution: Execution) -> None:
        batch, source, target = execution.batch, execution.source, execution.target
        with self.auth.database.session() as session:
            existing = session.scalar(select(Project).where(Project.project_code == batch.project_code))
            if existing and existing.id != batch.project_id:
                raise JobFailure("立项编号已被其他项目占用", retryable=False)
        source_exists, target_exists = fs.exists(source), fs.exists(target)
        if source_exists == target_exists or (source_exists and batch.ingest_step >= 3):
            raise JobFailure("临时目录或正式目录存在冲突，请核对原始项目的存放位置", retryable=False)
        if target_exists and batch.ingest_step < 3 and not self._marker(fs, target, batch.payload["ingest_move_id"]):
            raise JobFailure("目标同名目录无法确认属于本批次", retryable=False)
        parent = target.rsplit("/", 1)[0]
        if fs.exists(parent):
            if any(name != execution.folder and name.split("_")[0].casefold() == batch.project_code.casefold()
                   for name in fs.list(parent)):
                raise JobFailure("终审目录下已有同编号项目", retryable=False)
        # SMB 完整目标路径与归档生成路径都需满足命名规范。
        prefix = len(self.auth.config.nas.host) + len(self.auth.config.nas.share) + 4
        products = execution.snapshot.content["review"]["products"]
        product_count = len(products)
        frozen_files = {row["id"]: row for row in execution.snapshot.content["files"]}
        for item in products:
            evidence = execution.snapshot.content["evidence"].get(str(item["id"]), {})
            image = frozen_files.get(item["batch_file_id"])
            if (not image or not item["visual_ai_succeeded"] or not evidence.get("ai_model")
                    or evidence.get("ai_fingerprint") != image["fingerprint"]
                    or not matches_frozen_description(evidence, image["fingerprint"]) or set(item["tags"]) != set(FIXED_FIELDS)
                    or any(tag["confirmation_status"] == "pending" for tag in item["tags"].values())
                    or (item["kind"] == "set" and not item["relation_confirmed"])):
                raise JobFailure("审核快照的主图证据、标签或关系确认不完整", retryable=False)
        generated = [*REQUIRED, archive.HISTORY_DIR, archive.PROJECT_FILE, f"{archive.HISTORY_DIR}/{archive.PROJECT_FILE}.tmp", MARKER + ".tmp",
                     *(f"{directory}/{batch.project_code}-999.json.tmp" for directory in archive.PRODUCT_DIR.values())]
        content = execution.snapshot.content
        original_paths = [*content["directories"], *(r["rel_path"] for r in content["files"])]
        if (product_count not in range(1, 1000) or any(path.split("/")[0].casefold() in {MARKER.casefold(), (MARKER + ".tmp").casefold()}
                for path in original_paths) or any(prefix + len(safe_join(target, path)) > 200 for path in [*original_paths, *generated])):
            raise JobFailure("正式目标路径过长、商品数量异常或使用入库保留名", retryable=False)
        if set(REQUIRED) & {row["rel_path"] for row in content["files"]}:
            raise JobFailure("标准资料夹被同名文件占用", retryable=False)
        self._verify_files(fs, execution, target if target_exists else source, after_rename=target_exists)
        probe = target if target_exists else parent
        while not fs.exists(probe):
            if probe == self.paths.formal:
                raise JobFailure("NAS 正式区域尚未配置", retryable=False)
            probe = probe.rsplit("/", 1)[0]
        fs.check_write_directory(probe)

    def _parents(self, fs: NasFs, execution: Execution) -> None:
        current = self.paths.formal
        for part in execution.batch.final_path.split("/"):
            current = safe_join(current, part)
            archive.mkdir(fs, current)

    def _move(self, fs: NasFs, execution: Execution, context: JobContext) -> None:
        source, target = execution.source, execution.target
        if fs.exists(source):
            token = execution.batch.payload["ingest_move_id"]
            with self._nas_write(context):
                archive.write(fs, source, {MARKER: token})
                fs.rename(source, target)
        self._verify_files(fs, execution, target, after_rename=True)
        with self._nas_write(context):
            for name in REQUIRED:
                archive.mkdir(fs, safe_join(target, name))

    def _plan(self, session: Session, execution: Execution, times: dict[str, int]) -> None:
        batch, snapshot = execution.batch, execution.snapshot
        if session.get(IngestPlan, batch.id) is not None:
            return
        stamp = archive.utc_text(utcnow())
        content = snapshot.content
        review = content["review"]
        segment = batch.final_path.split("/")
        company, name = execution.folder.split("_")[1:]
        actors = {}
        for user_id in (batch.uploader_id, snapshot.submitted_by, batch.first_reviewer_id, batch.final_reviewer_id):
            user = session.get(User, user_id)
            actors[str(user_id)] = user.username if user else ""
        metadata = {"batch_id": batch.id, "round": batch.round, "snapshot_id": snapshot.id, "snapshot_sha256": snapshot.digest,
                    "uploader_id": batch.uploader_id, "submitted_by": snapshot.submitted_by,
                    "first_reviewer_id": batch.first_reviewer_id, "final_reviewer_id": batch.final_reviewer_id,
                    "actors": actors, "industry": segment[1][2:], "submitted_at": archive.utc_text(snapshot.submitted_at),
                    "reviewed_at": archive.utc_text(execution.final_review.decided_at), "source_company": review["batch"]["company_name"]}
        # 数据库内分配确定不重叠的区间，连同商品编号、档案映射一次提交。
        sequence = session.scalar(select(Setting).where(Setting.key == "catalog_identity_sequence"))
        maxima = [session.scalar(select(func.max(model.id))) or 0 for model in (Project, CatalogFile, Product)]
        start_id = max(*maxima, sequence.value if sequence else 0) + 1
        end_id = start_id + len(content["files"]) + len(review["products"])
        if end_id >= 2**53:
            raise JobFailure("项目、商品或文件的身份编号已用完", retryable=False)
        if sequence is None:
            session.add(Setting(key="catalog_identity_sequence", value=end_id, encrypted=False))
        else:
            sequence.value = end_id
        allocated_ids = iter(range(start_id, end_id + 1))
        project_id = next(allocated_ids)
        location = execution.final_review.path_change["after"]
        project = {"id": project_id, "project_code": batch.project_code, "code_source": "system_h" if re.fullmatch(r"H\d{4}-\d{4}", batch.project_code) else "company",
                   "company_name": company, "company_id": location["company_id"], "industry_id": session.scalar(
                       select(Industry.id).where(Industry.seq == int(segment[1][:2]), Industry.name == segment[1][2:])),
                   "won": location["won"], "project_name": name, "folder_name": execution.folder,
                   "current_path": execution.relative, "ingested_at": stamp, "archive_updated_at": stamp, "archive_review": metadata}
        files_by_batch = {}
        for row in content["files"]:
            path = row["rel_path"]
            head, separator, _ = path.partition("/")
            category = head if separator and head in (*REQUIRED, "套装", "单品") else "other"
            files_by_batch[row["id"]] = {"id": next(allocated_ids), "project_id": project_id, "rel_path": path,
                "size": row["size"], "mtime": times[path],
                "fingerprint": row["fingerprint"], "fingerprint_kind": "full", "category": category, "status": "active"}
        products, tags, relations, ids = [], [], [], {}
        ordered = sorted(review["products"], key=lambda row: (row["kind"] != "item", files_by_batch[row["batch_file_id"]]["rel_path"]))
        for index, row in enumerate(ordered, 1):
            batch_product = session.get(BatchProduct, row["id"])
            code = f"{batch.project_code}-{index:03d}"
            if batch_product.batch_id != batch.id or batch_product.product_code not in (None, code):
                raise JobFailure("已有商品编号或身份与本轮记录冲突", retryable=False)
            image = files_by_batch[row["batch_file_id"]]
            evidence = content["evidence"][str(row["id"])]
            product_id = next(allocated_ids)
            batch_product.product_code = code
            ids[row["id"]] = product_id
            products.append({"id": product_id, "product_code": code, "project_id": project_id, "kind": row["kind"], "display_name": row["display_name"],
                "file_id": image["id"], "archive_rel_path": f"{archive.PRODUCT_DIR[row['kind']]}/{code}.json", "status": "active",
                "free_text": row["free_text"], "vision_model": evidence["ai_model"], "ingested_at": stamp})
            for key, value in row["tags"].items():
                tags.append({"product_id": product_id, "field_key": key,
                             **{field: value[field] for field in archive.TAG_FIELDS if field not in ("product_id", "field_key")}})
        for row in ordered:
            for pair in row["relation_items"]:
                relations.append({"set_product_id": ids[row["id"]], "item_product_id": ids[pair["batch_product_id"]], "source": pair["source"],
                                  "confirmed_by": snapshot.submitted_by, "confirmed_at": archive.utc_text(snapshot.submitted_at),
                                  "status": "active", "inactive_reason": None, "relation_version": 1})
        files = list(files_by_batch.values())
        links = [{"kind": kind, "target_id": row["id"], "link_id": secrets.token_urlsafe(16)}
                 for kind, group in (("project", [project]), ("product", products), ("file", files)) for row in group]
        manifest = {"version": 2, "project": project, "files": files, "products": products,
                    "tags": tags, "relations": relations, "history": [], "links": links}
        archive.validate(manifest)
        session.add(IngestPlan(batch_id=batch.id, manifest=manifest))

    def _checkpoint(self, session: Session, batch: Batch, step: int) -> None:
        if batch.ingest_step < step:
            before = batch.ingest_step
            batch.ingest_step = step
            self._log(session, batch, "入库步骤完成", {"step": before}, {"step": step, "name": STEPS[step - 1]})

    @contextmanager
    def _nas_write(self, context: JobContext) -> Iterator[None]:
        """在写入边界核对角色和设备，避免注销或撤权后继续写入。"""
        with self.auth.lock:
            with self.auth.database.session() as session:
                self._for_job(session, context)
            yield

    def _run_step(self, context: JobContext, fs: NasFs, step: int) -> None:
        with self.auth.database.session() as session:
            execution = self._for_job(session, context)
            batch = execution.batch
            plan = session.get(IngestPlan, batch.id)
        times = {}
        if step == 4 and plan is None:
            times = {row["rel_path"]: int(fs.stat(safe_join(execution.target, row["rel_path"])).mtime * 1000)
                     for row in execution.snapshot.content["files"]}
        if step == 1:
            self._check(fs, execution)
        elif step == 2:
            with self._nas_write(context):
                self._parents(fs, execution)
        elif step == 3 and batch.ingest_step < 3:
            self._move(fs, execution, context)
        elif step in (5, 6):
            docs = archive.documents(plan.manifest)
            names = archive.PRODUCT_DIR.values() if step == 5 else (archive.HISTORY_DIR,)
            with self._nas_write(context):
                for name in names:
                    archive.mkdir(fs, safe_join(execution.target, name))
                archive.write(fs, execution.target,
                              {path: body for path, body in docs.items() if (path == archive.PROJECT_FILE) == (step == 6)})
        elif step == 8:
            with self.auth.database.session() as session:
                project = session.get(Project, batch.project_id)
                if archive.verify(session, project, fs, execution.target):
                    raise JobFailure("档案或永久链接与卡片柜不一致", retryable=False)
        elif step == 9:
            self._publish(context, execution, plan.manifest, fs)
            return
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            current = self._for_job(session, context)
            if step == 4:
                self._plan(session, current, times)
            if step == 7 and current.batch.project_id is None:
                current.batch.project_id = archive.register(session, plan.manifest, batch.id).id
            self._checkpoint(session, current.batch, step)
        if step == 3:
            marker = safe_join(execution.target, MARKER)
            if fs.exists(marker):
                with self._nas_write(context):
                    fs.delete(marker)

    def handle(self, context: JobContext) -> None:
        if context.fs is None:
            raise JobFailure("缺少终审本人 NAS 会话", retryable=False)
        for step in range(1, 10):
            try:
                self._run_step(context, context.fs, step)
            except archive.ArchiveError as error:
                raise JobFailure(str(error), retryable=False) from None
            except NasPermissionDenied:
                raise JobFailure("终审本人没有所需 NAS 目录权限", retryable=False) from None
            except AppError as error:
                raise JobFailure(error.message, retryable=False) from None

    def _publish(self, context: JobContext, execution: Execution, data: archive.Manifest, fs: NasFs) -> None:
        batch = execution.batch
        with self.auth.database.session() as session:
            model = vectors.model_for(self.ai, session)
            inputs = vectors.project_inputs(self.ai, session, data)
        vectors.generate(self.ai, batch.id, model, inputs)
        # 终审阶段锁住的是工作台内容；SMB 客户端的直接改动需要在发布前再次检查。
        self._verify_files(fs, execution, execution.target, after_rename=True)
        with self.auth.database.session() as session:
            if archive.verify(session, session.get(Project, batch.project_id), fs, execution.target):
                raise JobFailure("发布前档案或永久链接校验失败", retryable=False)
        self._cache_thumbs(batch, data, execution.snapshot.content["files"])
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            current = self._for_job(session, context).batch
            project = session.get(Project, current.project_id)
            if not archive.matches_catalog(session, project, data):
                raise JobFailure("卡片柜与已核验的入库清单不一致", retryable=False)
            vectors.publish_gate(self.ai, session, model, inputs)
            project.status = "active"
            current.status, current.ingest_error = DONE, None
            self._checkpoint(session, current, 9)
            self._log(session, current, "入库完成", {"status": INGESTING}, {"status": DONE, "project_id": project.id})
            self.notify.emit(session, "ingested", current)
            if self.on_published:
                self.on_published(session)
        publication.seal(self.auth, context, batch.id, execution.target)

    def _cache_thumbs(self, batch: Batch, data: archive.Manifest, frozen_files: list[dict]) -> None:
        root = Path(self.auth.config.paths.data_dir) / "thumbs"
        files = {row["id"]: row["rel_path"] for row in data["files"]}
        snapshot_files = {row["rel_path"]: row["id"] for row in frozen_files}
        for product in data["products"]:
            file_id = snapshot_files[files[product["file_id"]]]
            source = root / "batch" / str(batch.id) / str(file_id)
            target = root / "product" / str(product["id"])
            if source.is_dir() and not target.exists():
                temporary = target.with_suffix(".tmp")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, temporary, dirs_exist_ok=True)
                temporary.rename(target)
