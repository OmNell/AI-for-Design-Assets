"""管理员恢复计划、后台执行与报告。正式 NAS 会话仅授予读取。"""
from __future__ import annotations

from contextlib import closing
from sqlite3 import DatabaseError as SqliteError
from zipfile import BadZipFile

from alembic.util.exc import CommandError
from sqlalchemy.exc import DatabaseError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.database import Database
from app.db.models import BackupRecord, Job, Setting, User, utcnow
from app.errors import AppError
from app.services.ai.orchestrator import AiService
from app.services.backup.format import BACKUP_NAME, BackupFormatError
from app.services.backup.recovery import BackupRecoveryError, list_candidates, stage_from_nas
from app.services.crypto import CryptoError
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.maintenance.settings import ASSET_RECOVERY_CONFIRMED, SystemService
from app.services.nas import NasFs, NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable, Region
from app.services.nas.errors import NasLoginError
from app.services.nas.factory import create_paths, create_system_fs
from app.services.oplog import OplogService
from app.services.search.indexes import IndexService

from .incremental import RECOVERABLE, apply_incremental, plan_incremental
from .status import RecoveryStatus, Sample, sample_formal

RECOVERY_JOB = "asset_recovery"
APPLY_JOB = "asset_recovery_apply"
KINDS = (RECOVERY_JOB, APPLY_JOB)


class RecoveryAdmin:
    def __init__(self, status: RecoveryStatus, ai: AiService, indexes: IndexService,
                 queue: JobQueue, system: SystemService) -> None:
        self.status, self.ai, self.indexes, self.queue = status, ai, indexes, queue
        self.system = system
        self.auth = ai.auth
        self.database = ai.auth.database
        self.paths = create_paths(ai.auth.config)

    @staticmethod
    def view(job: Job) -> dict:
        data = job.payload
        return {"id": job.id, "action": data["action"], "actor": data["actor"], "status": job.status,
                "error": job.error, "started_at": job.started_at, "finished_at": job.finished_at,
                "progress": data.get("progress", {}), "summary": data.get("summary")}

    @staticmethod
    def job_query():
        return select(Job).where(Job.kind.in_(KINDS))

    def overview(self, actor: User) -> dict:
        state = self.status.local()
        with self.database.session() as session:
            self.queue._require_admin(session, actor)
            rows = list(session.scalars(self.job_query().order_by(Job.id.desc()).limit(16)))
            last_backup = session.scalar(select(func.max(BackupRecord.verified_at)))
            config = self.auth.config
            return {**state, "read_only": config.recovery.read_only, "last_backup_at": last_backup,
                    "can_backup": self.system.backup_allowed(session),
                    "runs": [self.view(row) for row in rows]}

    def report(self, actor: User, identifier: int) -> dict:
        with self.database.session() as session:
            self.queue._require_admin(session, actor)
            row = session.scalar(self.job_query().where(Job.id == identifier))
            if row is None or row.status != "done" or "result" not in row.payload:
                raise AppError("recovery_report_missing", "恢复报告尚未生成或不存在", 404)
            return row.payload["result"]

    def submit(self, actor: User, host: str, action: str, *, plan_id: int | None = None,
               backup_name: str | None = None) -> dict:
        with self.auth.lock, self.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self.queue._require_admin(session, actor)
            if session.scalar(self.job_query().where(Job.status.in_(("queued", "running"))).limit(1)) is not None:
                raise AppError("recovery_busy", "已有恢复任务正在等待或执行", 409)
            payload = {"actor_id": actor.id, "actor": actor.username, "host": host, "action": action}
            if action == "apply":
                plan = session.scalar(self.job_query().where(Job.id == plan_id, Job.status == "done"))
                if plan is None or plan.payload["action"] != "scan" or not plan.payload.get("summary", {}).get("recoverable"):
                    raise AppError("recovery_plan_required", "请先生成含可恢复资产的正式档案计划", 409)
                payload["plan_id"] = plan_id
            if action == "stage":
                if not backup_name or not BACKUP_NAME.fullmatch(backup_name):
                    raise AppError("invalid_backup_name", "请选择有效的工作台备份文件名", 422)
                payload["backup_name"] = backup_name
            row = self.queue.enqueue(APPLY_JOB if action == "apply" else RECOVERY_JOB, payload, session=session)
            OplogService(session).write(actor.id, "admin", "请求资产恢复操作", "job", str(row.id),
                                        None, {"action": action, "plan_id": plan_id}, "成功", host)
            return self.view(row)

    def _persist(self, job: Job, *, progress: dict, result: dict | None = None, summary: dict | None = None) -> None:
        with self.database.session() as session:
            row = session.get(Job, job.id)
            if row is not None and row.status == "running" and row.attempts == job.attempts:
                row.payload = {**row.payload, "progress": progress}
                if result is not None:
                    row.payload = {**row.payload, "result": result, "summary": summary}

    @staticmethod
    def clean(plan: dict) -> bool:
        return not (plan["unresolved"] or plan["inaccessible"] or any(row["kind"] != "same" for row in plan["items"]))

    @staticmethod
    def _set_confirmation(session: Session, confirmed: bool) -> None:
        row = session.scalar(select(Setting).where(Setting.key == ASSET_RECOVERY_CONFIRMED))
        if row is None:
            session.add(Setting(key=ASSET_RECOVERY_CONFIRMED, value=confirmed, encrypted=False))
        else:
            row.value = confirmed

    def _scan(self, fs: NasFs, root: str) -> tuple[dict, dict]:
        plan = plan_incremental(self.database, fs, root)
        summary = {"recoverable": sum(row["kind"] in RECOVERABLE for row in plan["items"]),
                   "conflicts": sum(row["kind"] in ("conflict", "local_only") for row in plan["items"]),
                   "unresolved": len(plan["unresolved"]) + len(plan["inaccessible"])}
        with self.auth.lock, self.database.session() as session:
            self._set_confirmation(session, self.clean(plan))
        return plan, summary

    def _stage(self, fs: NasFs, job: Job, root: str) -> tuple[dict, dict]:
        config = self.auth.config
        folder = config.paths.data_dir.with_name(config.paths.data_dir.name + "-recovery")
        if folder.is_symlink() or folder.is_junction():
            raise JobFailure("恢复暂存目录必须是本机普通目录", retryable=False)
        folder.mkdir(mode=0o700, parents=False, exist_ok=True)
        target = folder / f"backup-{job.id}-{job.attempts}"
        report = stage_from_nas(fs, self.paths.root(Region.BACKUP), job.payload["backup_name"], target,
                                destination_config=config)
        self._persist(job, progress={"phase": "validating"})
        try:
            with closing(Database(target / "data")) as database:
                difference = plan_incremental(database, fs, root)
        except (NasNotFound, NasPermissionDenied, NasSessionExpired, NasUnavailable):
            difference = None
        return {**report, "stage_dir": str(target), "difference": difference}, {
            "unresolved": int(difference is None)
        }

    def _detect(self, fs: NasFs, root: str) -> tuple[dict, dict]:
        try:
            self.status.sample = sample_formal(fs, root, self.status.stopping)
        except NasNotFound:
            self.status.sample = Sample("formal_root_missing", utcnow())
        except NasPermissionDenied:
            self.status.sample = Sample("system_access_required", utcnow())
        except (NasSessionExpired, NasUnavailable):
            self.status.sample = Sample("nas_unavailable", utcnow())
        try:
            backups = list_candidates(fs, self.paths.root(Region.BACKUP), verify_manifest=True)
            backup_state = "checked"
        except (NasPermissionDenied, NasNotFound, NasSessionExpired, NasUnavailable):
            backups, backup_state = [], "unavailable"
        return {"backups": backups, "backup_state": backup_state, "nas_state": self.status.sample.state}, {
            "backup_candidates": sum(row["validation"] == "manifest_checked" for row in backups)
        }

    def handle(self, context: JobContext) -> None:
        job = context.job
        data = job.payload
        action = data["action"]
        with self.database.session() as session:
            actor = session.get(User, data["actor_id"])
            if actor is None or not actor.enabled or "admin" not in actor.roles:
                raise JobFailure("发起管理员的权限已失效", retryable=False)
            plan = session.get(Job, data["plan_id"]).payload["result"] if action == "apply" else None
        self._persist(job, progress={"phase": "recovering" if action in ("apply", "stage") else "detecting"})
        config = self.auth.config
        protected = config.model_copy(update={"recovery": config.recovery.model_copy(update={"read_only": True})})
        root = self.paths.root(Region.FORMAL)
        try:
            with closing(create_system_fs(protected)) as fs:
                if action == "scan":
                    result, summary = self._scan(fs, root)
                elif action == "apply":
                    result = apply_incremental(self.ai, fs, root, plan, actor_id=actor.id, host=data["host"],
                        on_progress=lambda value: self._persist(job, progress=value),
                        on_publish=self.indexes.sync_after_publish)
                    summary = {"recovered": len(result["recovered"]), "failed": len(result["blocked"]),
                               "unresolved": len(result["unresolved"]) + len(result["inaccessible"]),
                               "conflicts": sum(row["kind"] in ("conflict", "local_only") for row in plan["items"])}
                elif action == "stage":
                    result, summary = self._stage(fs, job, root)
                else:
                    result, summary = self._detect(fs, root)
        except CryptoError:
            raise JobFailure("恢复账号的密文无法解密，请核对原密钥或重新配置账号", retryable=False) from None
        except NasLoginError as error:
            raise JobFailure(str(error), retryable=False) from None
        except NasPermissionDenied:
            raise JobFailure("系统账号无权读取所需 NAS 路径，请核对正式区和备份区权限", retryable=False) from None
        except (CommandError, SqliteError, DatabaseError):
            raise JobFailure("备份数据库完整性检查或迁移失败，原运行库保持原状", retryable=False) from None
        except OSError:
            raise JobFailure("本机暂存目录不可写，请使用停机部署恢复工具", retryable=False) from None
        except (BackupRecoveryError, BackupFormatError) as error:
            raise JobFailure(str(error), retryable=False) from None
        except (ValueError, BadZipFile):
            message = ("无法验证或暂存备份，请核对文件完整性、数据库兼容性和目标环境配置"
                       if action == "stage" else "档案校验未完成，请核对 NAS 状态后重试")
            raise JobFailure(message, retryable=False) from None
        incomplete = any(summary.get(key) for key in ("failed", "unresolved", "conflicts"))
        self._persist(job, progress={"phase": "partial" if incomplete else "complete"}, result=result, summary=summary)
