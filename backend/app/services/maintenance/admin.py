"""后台查询和权限受控的维护动作。"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import csv
import json
from io import StringIO
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (AiAlert, BackupRecord, Batch, CatalogFile, IngestPlan, InspectionFinding, InspectionRun,
                           Job, Oplog, Product, ProductEmbedding, Project, SearchClick, SearchLog, User, utcnow)
from app.errors import AppError
from app.schemas.jobs import JobResponse
from app.schemas.maintenance import (BackupView, ErrorItem, FindingView, LogFilter, LogPage, LogView,
    RepairCandidate, RepairResult, RunView, SearchLogPage, SearchLogView, ThumbStatus)
from app.schemas.postops import OperationBody
from app.services.auth.service import AuthService, LoginContext
from app.services.backup.format import measure
from app.services.catalog.media import MediaService
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.maintenance.settings import CST
from app.services.nas import NasNotFound, NasPermissionDenied, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.postops.drafts import DraftService
from app.services.postops.supplement import active_batch_id
from app.services.thumbs.images import ImageDecodeError, VARIANT_SIZES, render_variants
from app.services.upload.service import UploadService


def csv_cell(value: object) -> object:
    if isinstance(value, str) and (value[:1] in ("\t", "\r", "\n") or value.lstrip()[:1] in ("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + value
    return value


def csv_export(header: tuple[str, ...], rows: Iterator[tuple]) -> str:
    data = StringIO(newline="")
    data.write("\ufeff")
    writer = csv.writer(data)
    writer.writerow(header)
    writer.writerows(tuple(csv_cell(value) for value in row) for row in rows)
    return data.getvalue()


def inspection_dates(start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    if start_date and end_date and start_date > end_date:
        raise AppError("invalid_inspection_dates", "巡检开始日期不能晚于结束日期", 422)
    start = datetime.combine(start_date, time.min, CST).astimezone(timezone.utc) if start_date and start_date != date.min else None
    end = datetime.combine(end_date, time.max, CST).astimezone(timezone.utc) if end_date else None
    return start, end


class AdminService:
    def __init__(self, auth: AuthService, queue: JobQueue, draft: DraftService, media: MediaService) -> None:
        self.auth, self.queue, self.draft, self.media = auth, queue, draft, media
        self.paths = create_paths(auth.config)

    def runs(self, actor: User, page: int, start_date: date | None, end_date: date | None) -> list[RunView]:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            start, end = inspection_dates(start_date, end_date)
            query = select(InspectionRun)
            if start is not None:
                query = query.where(InspectionRun.started_at >= start)
            if end is not None:
                query = query.where(InspectionRun.started_at <= end)
            return [RunView.model_validate(row) for row in session.scalars(
                query.order_by(InspectionRun.started_at.desc(), InspectionRun.id.desc()).offset((page - 1) * 100).limit(100))]

    def findings(self, actor: User, run_id: int | None, kind: str | None, project: int | None, status: str,
                 page: int, start_date: date | None, end_date: date | None) -> list[FindingView]:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            query = select(InspectionFinding)
            start, end = inspection_dates(start_date, end_date)
            if start is not None or end is not None:
                query = query.outerjoin(InspectionRun, InspectionRun.id == InspectionFinding.run_id)
                inspected_at = func.coalesce(InspectionRun.started_at, InspectionFinding.created_at)
                if start is not None:
                    query = query.where(inspected_at >= start)
                if end is not None:
                    query = query.where(inspected_at <= end)
            if run_id:
                query = query.where(InspectionFinding.run_id == run_id)
            if kind:
                query = query.where(InspectionFinding.kind == kind)
            if project:
                query = query.where(InspectionFinding.project_id == project)
            if status == "open":
                query = query.where(InspectionFinding.acknowledged_at.is_(None), InspectionFinding.resolved_at.is_(None))
            if status == "ack":
                query = query.where(InspectionFinding.acknowledged_at.is_not(None))
            if status == "repaired":
                query = query.where(InspectionFinding.resolved_at.is_not(None))
            result = session.scalars(query.order_by(InspectionFinding.id.desc()).offset((page-1)*100).limit(100))
            return [FindingView.model_validate(row) for row in result]

    def finding(self, actor: User, identifier: int) -> FindingView:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            row = session.get(InspectionFinding, identifier)
            if row is None:
                raise AppError("finding_not_found", "巡检记录不存在", 404)
            return FindingView.model_validate(row)

    def ack(self, actor: User, identifier: int, host: str) -> FindingView:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self.queue._require_admin(session, actor)
            row = session.get(InspectionFinding, identifier)
            if row is None:
                raise AppError("finding_not_found", "巡检记录不存在", 404)
            if row.acknowledged_at is None:
                row.acknowledged_at, row.acknowledged_by = utcnow(), actor.id
                OplogService(session).write(actor.id, "admin", "知悉巡检异常", "inspection_finding", str(row.id),
                                            None, {"kind": row.kind, "project_id": row.project_id}, "成功", host)
            return FindingView.model_validate(row)

    def backups(self, actor: User) -> list[BackupView]:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            query = select(BackupRecord, Job).join(Job).order_by(BackupRecord.id.desc()).limit(100)
            return [BackupView(id=row.id, job_id=row.job_id, name=row.name, status=job.status, error=job.error,
                        size=row.size, verified_at=row.verified_at, local_retained=row.local_retained, created_at=row.created_at)
                    for row, job in session.execute(query)]

    def download_backup(self, actor: User, identifier: int, host: str):
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            row = session.get(BackupRecord, identifier)
            if row is None or row.verified_at is None:
                raise AppError("backup_not_verified", "已核验的备份不存在", 404)
        fs = self.auth.pool.system()
        stream = fs.open_read(self.paths.join(Region.BACKUP, row.name))
        try:
            if measure(stream) != (row.size, row.fingerprint):
                raise AppError("backup_corrupt", "备份下载前完整性检查失败", 409)
            stream.seek(0)
            with self.auth.database.session() as session:
                self.queue._require_admin(session, actor)
                OplogService(session).write(actor.id, "admin", "下载已验证备份", "backup", str(row.id),
                                            None, {"name": row.name}, "成功", host)
        except BaseException:
            stream.close()
            raise
        return row, stream

    @staticmethod
    def _date_filter(query, model, filters: LogFilter):
        for value, compare in ((filters.since, model.ts.__ge__), (filters.until, model.ts.__lt__)):
            if value:
                query = query.where(compare(value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value))
        return query.where(model.user_id == filters.user_id) if filters.user_id else query

    def logs(self, actor: User, filters: LogFilter, export: bool = False) -> LogPage | str:
        query = self._date_filter(select(Oplog, User.username).outerjoin(User), Oplog, filters)
        for name in ("action", "object_type", "object_id", "result"):
            if value := getattr(filters, name):
                query = query.where(getattr(Oplog, name) == value)
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            ordered = query.order_by(Oplog.id.desc())
            if export:
                rows = session.execute(ordered)
                return csv_export(("时间", "账号", "角色", "操作", "对象类型", "对象 ID", "结果", "来源", "变更前", "变更后"),
                    ((row.ts, username, row.role, row.action, row.object_type, row.object_id, row.result, row.client_host,
                      json.dumps(row.before, ensure_ascii=False), json.dumps(row.after, ensure_ascii=False))
                     for row, username in rows))
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            items = [LogView.model_validate(row).model_copy(update={"username": username}) for row, username in
                     session.execute(ordered.offset((filters.page-1)*filters.limit).limit(filters.limit))]
            return LogPage(total=total, items=items)

    def searches(self, actor: User, filters: LogFilter, export: bool = False) -> SearchLogPage | str:
        clicks = select(func.count(SearchClick.id)).where(SearchClick.search_id == SearchLog.id).correlate(SearchLog).scalar_subquery()
        query = self._date_filter(select(SearchLog, User.username, clicks).outerjoin(User), SearchLog, filters)
        if filters.unclicked or export:
            query = query.where(clicks == 0)
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            ordered = query.order_by(SearchLog.id.desc())
            if export:
                return csv_export(("搜索时间", "账号", "查询", "结果数", "点击", "毫秒", "降级提示", "检索模式"),
                    ((row.ts, username, row.query, row.result_count, count, row.duration_ms, "、".join(row.warnings), "快速匹配" if row.mode == "quick" else "AI 自然语言搜索")
                     for row, username, count in session.execute(ordered)))
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            items = [SearchLogView(id=row.id, ts=row.ts, user_id=row.user_id, username=username, query=row.query, mode=row.mode,
                result_count=row.result_count, clicks=count, duration_ms=row.duration_ms, warnings=row.warnings)
                for row, username, count in session.execute(ordered.offset((filters.page-1)*filters.limit).limit(filters.limit))]
            return SearchLogPage(total=total, items=items)

    def errors(self, actor: User) -> list[ErrorItem]:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            errors = []
            for job in session.scalars(select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(100)):
                payload = job.payload if isinstance(job.payload, dict) else {}
                errors.append(ErrorItem(kind=job.kind, reason=job.error or "任务失败", object_id=job.id, job_id=job.id,
                    batch_id=payload.get("batch_id"), generation=payload.get("generation"),
                    retry="system" if job.kind in ("inspection", "backup") else
                    "candidate" if job.kind == "embed_index" else "actor"))
            for alert in session.scalars(select(AiAlert).order_by(AiAlert.id.desc()).limit(100)):
                errors.append(ErrorItem(kind="ai_stalled", reason=alert.reason, object_id=alert.id, batch_id=alert.batch_id))
            for batch in session.scalars(select(Batch).where(Batch.status.in_(("入库异常", "AI 处理失败"))).limit(100)):
                errors.append(ErrorItem(kind=batch.kind, reason=batch.ingest_error or batch.status,
                                        object_id=batch.id, batch_id=batch.id, retry="actor"))
            for row in session.scalars(select(ProductEmbedding).where(ProductEmbedding.state == "failed").limit(100)):
                errors.append(ErrorItem(kind="embedding", reason=row.error or "向量生成失败", object_id=row.product_id,
                                        generation=row.index_generation))
            for row in session.scalars(select(InspectionFinding).where(InspectionFinding.resolved_at.is_(None),
                    InspectionFinding.acknowledged_at.is_(None)).order_by(InspectionFinding.id.desc()).limit(100)):
                errors.append(ErrorItem(kind=row.kind, reason=row.detail, object_id=row.id))
            return errors

    def repairs(self, login: LoginContext) -> list[RepairCandidate]:
        with self.auth.database.session() as session:
            UploadService._check_login(session, login)
            actor = session.get(User, login.user.id)
            if not actor or not actor.enabled or "final_reviewer" not in actor.roles:
                raise AppError("forbidden", "只有终审可以发起档案修复申请", 403)
            fs = self.auth.pool.get(login.session_id)
            query = select(InspectionFinding, Project).join(Project).where(InspectionFinding.kind == "archive_broken",
                InspectionFinding.resolved_at.is_(None)).order_by(InspectionFinding.id.desc())
            result = {}
            for row, project in session.execute(query):
                if project.id in result or project.status not in ("active", "voided", "deleted"):
                    continue
                try:
                    exists = fs.stat(self.paths.join(Region.FORMAL, project.current_path)).is_dir
                except (NasPermissionDenied, NasNotFound):
                    continue
                if exists:
                    result[project.id] = RepairCandidate(project_id=project.id, project_code=project.project_code,
                        project_name=project.project_name, ready=active_batch_id(session, project.id) is None, detail=row.detail)
            return list(result.values())

    def request_repairs(self, ids: list[int], login: LoginContext, host: str) -> list[RepairResult]:
        with self.auth.database.session() as session:
            UploadService._check_login(session, login)
            actor = session.get(User, login.user.id)
            if actor is None or not actor.enabled or "final_reviewer" not in actor.roles:
                raise AppError("forbidden", "只有终审可以发起档案修复申请", 403)
        if not ids:
            ids = [row.project_id for row in self.repairs(login) if row.ready]
        results = []
        for identifier in dict.fromkeys(ids):
            try:
                response = self.draft.create(identifier, "archive_repair", OperationBody(params={}), login, host)
                results.append(RepairResult(project_id=identifier, batch_id=response.batch_id, error=None))
            except (AppError, NasPermissionDenied, NasNotFound) as exc:
                if isinstance(exc, AppError) and exc.status_code == 403:
                    raise
                results.append(RepairResult(project_id=identifier, batch_id=None,
                                             error=exc.message if isinstance(exc, AppError) else "NAS 项目目录不可用"))
        return results

    @staticmethod
    def _thumbnail_assets():
        return select(Product.id, CatalogFile).join(Project, Project.id == Product.project_id).join(
            CatalogFile, CatalogFile.id == Product.file_id).where(
                Product.status == "active", Project.status == "active", CatalogFile.status == "active",
                CatalogFile.project_id == Project.id)

    @staticmethod
    def _thumbnail_job_products(session: Session, statuses: tuple[str, ...]) -> set[int]:
        return set(session.scalars(select(func.json_extract(Job.payload, "$.product_id")).where(
            Job.kind == "catalog_thumb", Job.status.in_(statuses))))

    def _thumbnail_checks(self, session: Session) -> Iterator[tuple[int, bool]]:
        plans: dict[int, IngestPlan | None] = {}
        for pid, file in session.execute(self._thumbnail_assets().order_by(Product.id).execution_options(yield_per=100)):
            batch_id = file.first_batch_id
            if batch_id is not None and batch_id not in plans:
                plans[batch_id] = session.get(IngestPlan, batch_id)
            yield pid, not self.media.missing_sizes(file, pid, plans.get(batch_id))

    def thumbnail_status(self, actor: User) -> ThumbStatus:
        with self.auth.database.session() as session:
            self.queue._require_admin(session, actor)
            running = self._thumbnail_job_products(session, ("queued", "running"))
            ready = missing = 0
            for _, valid in self._thumbnail_checks(session):
                ready += int(valid)
                missing += int(not valid)
            return ThumbStatus(total=ready + missing, ready=ready, missing=missing, running=len(running))

    def rebuild_thumbs(self, ids: list[int], login: LoginContext, host: str, *,
                       missing_only: bool = False, all_missing: bool = False) -> list[JobResponse]:
        if all_missing:
            if ids:
                raise AppError("thumbnail_selection_invalid", "批量恢复与指定商品只可选择一项", 422)
            with self.auth.database.session() as session:
                UploadService._check_login(session, login)
                self.queue._require_admin(session, login.user)
                running = self._thumbnail_job_products(session, ("queued", "running"))
                failed = self._thumbnail_job_products(session, ("failed",))
                ids = []
                retries = []
                for pid, valid in self._thumbnail_checks(session):
                    if valid or pid in running:
                        continue
                    if pid in failed:
                        if len(retries) < 100:
                            retries.append(pid)
                    else:
                        ids.append(pid)
                    if len(ids) == 100:
                        break
                # 优先覆盖还未执行的商品，少数损坏图片不会挡住后续批次。
                ids.extend(retries[:100 - len(ids)])
            missing_only = True
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self.queue._require_admin(session, login.user)
            UploadService._check_login(session, login)
            if not ids and not all_missing:
                raise AppError("products_required", "请输入要重建的商品 ID", 422)
            active = set(session.scalars(self._thumbnail_assets().where(Product.id.in_(ids))))
            if not all_missing and set(ids) != active:
                raise AppError("product_not_found", "包含无效的正式商品", 404)
            selected = sorted(active - self._thumbnail_job_products(session, ("queued", "running")))
            jobs = [self.queue.enqueue("catalog_thumb", {"product_id": pid, "missing_only": missing_only}, login.user.id,
                                         session=session, run_as_session_id=login.session_id) for pid in selected]
            if not selected:
                return []
            OplogService(session).write(login.user.id, "admin", "请求重建商品缩略图", "thumb", None,
                                        None, {"ids": selected, "missing_only": missing_only}, "成功", host)
            return [JobResponse.model_validate(job) for job in jobs]

    def rebuild_worker(self, context: JobContext) -> None:
        with self.auth.database.session() as session:
            user = session.get(User, context.job.run_as_user_id)
            if user is None or not user.enabled or "admin" not in user.roles or not context.job.run_as_session_id:
                raise JobFailure("管理员登录已失效", retryable=False)
        login = LoginContext(user, context.job.run_as_session_id)
        pid = context.job.payload["product_id"]
        try:
            file_id = self.media.main_image_id(pid)
            with self.media.open(file_id, login, "缩略图重建") as asset:
                if asset.warning:
                    raise JobFailure("主图发生漂移，须先人工核对", retryable=False)
                sizes = VARIANT_SIZES
                if context.job.payload.get("missing_only"):
                    file = asset.reference.file
                    with self.auth.database.session() as session:
                        plan = session.get(IngestPlan, file.first_batch_id) if file.first_batch_id is not None else None
                    sizes = self.media.missing_sizes(file, pid, plan)
                if sizes:
                    render_variants(asset.stream, self.media.thumbnail_directory(file_id, asset.digest), sizes)
        except (ImageDecodeError, AppError) as exc:
            raise JobFailure(exc.message if isinstance(exc, AppError) else str(exc), retryable=False) from None
