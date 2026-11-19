"""以管理员个人 NAS 会话预览和确认临时区清理。"""
from __future__ import annotations

from datetime import timedelta
import hashlib
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Batch, BatchFile, CleanupPlan, Job, PostopPlan, User, utcnow
from app.errors import AppError
from app.services.auth.service import AuthService, LoginContext
from app.services.inspection import walk
from app.services.nas import NasFs, Region, safe_join
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService
from app.services.upload.fingerprint import fingerprint
from app.services.upload.service import UploadService

TERMINAL = {"已入库", "已终止", "已撤回"}


class CleanupService:
    def __init__(self, auth: AuthService) -> None:
        self.auth, self.paths = auth, create_paths(auth.config)

    def _actor(self, session: Session, login: LoginContext) -> NasFs:
        UploadService._check_login(session, login)
        user = session.get(User, login.user.id)
        if user is None or not user.enabled or "admin" not in user.roles:
            raise AppError("forbidden", "只有管理员可以清理临时区", 403)
        return self.auth.pool.get(login.session_id)

    @staticmethod
    def _busy(session: Session, target: str, batch: Batch | None) -> bool:
        if batch is not None:
            if batch.status not in TERMINAL or batch.lock_active:
                return True
            if session.scalar(select(Job.id).where(Job.status.in_(("queued", "running")),
                    func.json_extract(Job.payload, "$.batch_id") == batch.id).limit(1)) is not None:
                return True
        for plan, owner in session.execute(select(PostopPlan, Batch).join(Batch)):
            if batch is not None and owner.status in TERMINAL and not owner.lock_active:
                continue
            if any(ref["region"] == "temp" and (ref["path"] == target or ref["path"].startswith(target + "/"))
                   for ref in plan.sources.values()):
                return True
        return False

    @staticmethod
    def _key(path: str) -> str:
        return hashlib.sha256(path.encode()).hexdigest()

    @staticmethod
    def _batches(session: Session) -> dict[str, Batch]:
        return {row.batch_no.casefold(): row for row in session.scalars(select(Batch).where(Batch.temp_path != ""))}

    def _targets(self, session: Session, fs: NasFs) -> Iterator[tuple[str, Batch | None]]:
        roots = self._batches(session)
        base = self.paths.root(Region.TEMP)
        for name in fs.list(base):
            relative = safe_join(name)
            batch = roots.get(relative.casefold())
            if batch and relative == batch.batch_no:
                yield relative, batch
            elif batch is None:
                # 未登记目录中的每个文件也须单独核对，目录本身不做递归清理。
                if fs.stat(self.paths.join(Region.TEMP, relative)).is_dir:
                    for path, info in walk(fs, base, relative):
                        if not info.is_dir:
                            yield path, None
                else:
                    yield relative, None

    def _entries(self, fs: NasFs, session: Session, target: str, batch: Batch | None, *, digest: bool) -> list[dict]:
        root = self.paths.root(Region.TEMP)
        absolute = self.paths.join(Region.TEMP, target)
        stat = fs.stat(absolute)
        if batch is None:
            if stat.is_dir:
                raise AppError("cleanup_unproven", "未知目录无法证明无活动引用", 409)
            records = [(target, stat)]
            registered = {target}
        else:
            folder = safe_join(batch.temp_path)
            if folder.split("/")[0] != target or not stat.is_dir:
                raise AppError("cleanup_unproven", "临时位置与批次登记不一致", 409)
            registered = {safe_join(folder, row) for row in session.scalars(select(BatchFile.rel_path).where(BatchFile.batch_id == batch.id))}
            records = [(name, info) for name, info in walk(fs, root, target) if not info.is_dir]
        manifest = []
        for path, stat in records:
            if digest and path not in registered:
                raise AppError("cleanup_unproven", "存在未登记的临时文件，已停止清理", 409)
            sha = fingerprint(fs, self.paths.join(Region.TEMP, path), stat.size) if digest else ""
            manifest.append({"path": path, "size": stat.size, "mtime": stat.mtime, "fingerprint": sha})
        return sorted(manifest, key=lambda entry: entry["path"])

    def listing(self, login: LoginContext) -> list[dict]:
        with self.auth.database.session() as session:
            fs = self._actor(session, login)
            entries = []
            for target, batch in self._targets(session, fs):
                files = self._entries(fs, session, target, batch, digest=False)
                owner = session.get(User, batch.owner_id) if batch else None
                entries.append({"key": self._key(target), "label": target, "batch_id": batch.id if batch else None,
                    "project_code": batch.project_code if batch else None, "kind": batch.kind if batch else "orphan",
                    "status": batch.status if batch else "待确认孤儿文件", "owner": (owner.display_name or owner.username) if owner else None,
                    "created_at": batch.created_at if batch else None, "updated_at": batch.updated_at if batch else None,
                    "eligible": bool(files) and not self._busy(session, target, batch), "files": len(files),
                    "size": sum(row["size"] for row in files)})
            return entries

    def preview(self, key: str, login: LoginContext, host: str) -> CleanupPlan:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            fs = self._actor(session, login)
            candidate = next(((path, batch) for path, batch in self._targets(session, fs) if self._key(path) == key), None)
            if candidate is None:
                raise AppError("cleanup_not_found", "临时文件或批次不存在", 404)
            target, batch = candidate
            if self._busy(session, target, batch):
                raise AppError("cleanup_active", "活动或异常待恢复批次禁止清理", 409)
            entries = self._entries(fs, session, target, batch, digest=True)
            if not entries:
                raise AppError("cleanup_empty", "没有可清理的临时文件", 409)
            row = CleanupPlan(user_id=login.user.id, session_id=login.session_id,
                              batch_id=batch.id if batch else None, target=target, entries=entries)
            session.add(row)
            session.flush()
            self._log(session, login, "登记临时区清理清单", row, "成功", host)
            return row

    @staticmethod
    def _log(session: Session, login: LoginContext, action: str, plan: CleanupPlan, result: str, host: str) -> None:
        OplogService(session).write(login.user.id, "admin", action, "cleanup", str(plan.id), None,
            {"batch_id": plan.batch_id, "target": plan.target, "entries": plan.entries}, result, host)

    def execute(self, plan_id: int, confirmation: str, login: LoginContext, host: str) -> CleanupPlan:
        with self.auth.lock:
            with self.auth.database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                fs = self._actor(session, login)
                row = session.get(CleanupPlan, plan_id)
                if row is None or (row.user_id, row.session_id) != (login.user.id, login.session_id):
                    raise AppError("cleanup_not_found", "本设备没有该清理清单", 404)
                if row.confirmed_at is not None or utcnow() - timedelta(minutes=15) > row.created_at.replace(tzinfo=utcnow().tzinfo):
                    raise AppError("cleanup_stale", "清理清单已使用或过期，请重新预览", 409)
                if confirmation != row.target:
                    raise AppError("cleanup_confirmation_required", "输入的名称与清单不一致", 422)
                batch = session.get(Batch, row.batch_id) if row.batch_id else None
                referenced = self._batches(session).get(row.target.split("/", 1)[0].casefold())
                if referenced != batch or self._busy(session, row.target, batch):
                    raise AppError("cleanup_active", "当前状态或文件归属已变化，禁止清理", 409)
                if self._entries(fs, session, row.target, batch, digest=True) != row.entries:
                    raise AppError("cleanup_stale", "实际文件与确认清单不一致，请重新预览", 409)
                row.confirmed_at = utcnow()
                self._log(session, login, "确认临时区清理", row, "成功", host)
            try:
                with self.auth.database.session() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    fs = self._actor(session, login)
                    batch = session.get(Batch, row.batch_id) if row.batch_id else None
                    if self._batches(session).get(row.target.split("/", 1)[0].casefold()) != batch or self._busy(session, row.target, batch):
                        raise AppError("cleanup_active", "清理前批次状态或文件引用已变化", 409)
                    for entry in row.entries:
                        path = self.paths.join(Region.TEMP, entry["path"])
                        info = fs.stat(path)
                        if info.is_dir or (info.size, info.mtime) != (entry["size"], entry["mtime"]) or (
                                fingerprint(fs, path, entry["size"]) != entry["fingerprint"]):
                            raise AppError("cleanup_stale", "实际文件已变化，停止删除", 409)
                        fs.delete(path)
            except Exception:
                with self.auth.database.session() as session:
                    self._log(session, login, "临时区清理中断", row, "失败", host)
                raise
            with self.auth.database.session() as session:
                self._log(session, login, "完成临时区清理", row, "成功", host)
        return row
