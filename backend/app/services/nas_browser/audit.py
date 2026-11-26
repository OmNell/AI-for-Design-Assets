"""记录工作根下未登记文件的逻辑访问及实际发送范围。"""
from datetime import timedelta
from hashlib import sha256
import secrets
from typing import Literal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.models import AccessLog, utcnow
from app.schemas.nas_browser import FileEntry
from app.services.auth.service import AuthService, LoginContext
from app.services.oplog import OplogService

AccessKind = Literal["nas_download", "nas_preview"]
ACTIONS = {"nas_download": "NAS 文件下载", "nas_preview": "NAS 文件预览"}


class NasAccessAudit:
    def __init__(self, auth: AuthService) -> None:
        self.database = auth.database

    @staticmethod
    def _scope(login: LoginContext, kind: AccessKind, path: str | None) -> Select[tuple[AccessLog]]:
        # 实时 NAS 路径没有正式文件 ID，使用独立访问类型与相对路径。
        return select(AccessLog).where(AccessLog.session_id == login.session_id, AccessLog.kind == kind,
                                       AccessLog.target_id == 0, AccessLog.target_path == path)

    @staticmethod
    def _create(session: Session, login: LoginContext, kind: AccessKind, host: str, path: str | None,
                logical_id: str, outcome: str, delivery: str | None = None) -> AccessLog:
        row = AccessLog(user_id=login.user.id, session_id=login.session_id, kind=kind, target_id=0,
                        target_path=path, logical_id=logical_id, client_host=host, outcome=outcome,
                        delivery=delivery, delivery_at=utcnow() if delivery else None)
        session.add(row)
        session.flush()
        result = "开始" if delivery == "sending" else "成功" if outcome == "allowed" else "异常"
        OplogService(session).write(login.user.id, ",".join(login.user.roles), ACTIONS[kind], "nas_file", str(row.id),
                                    None, {"outcome": outcome, **({"path": path} if path else {})}, result, host)
        return row

    def record(self, login: LoginContext, kind: AccessKind, host: str, path: str | None, outcome: str,
               transfer: str | None = None) -> None:
        with self.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            query = self._scope(login, kind, path).where(AccessLog.outcome == outcome)
            if transfer:
                logical_id = sha256(f"{path or ''}\n{outcome}\n{transfer}".encode()).hexdigest()
                if session.scalar(query.where(AccessLog.logical_id == logical_id)) is not None:
                    return
            else:
                logical_id = secrets.token_hex(16)
                if kind == "nas_preview" and path is not None:
                    cutoff = utcnow() - timedelta(minutes=1)
                    if session.scalar(query.where(AccessLog.ts >= cutoff).limit(1)) is not None:
                        return
            self._create(session, login, kind, host, path, logical_id, outcome)

    def start(self, login: LoginContext, item: FileEntry, host: str, transfer: str | None, resume: bool) -> int:
        # 通过句柄取得的路径、大小与修改时间区分版本。关联标识只以摘要落盘。
        version = sha256(f"{item.path}\n{item.size}\n{item.modified_at.isoformat()}".encode()).hexdigest()
        prefix = version + "."
        suffix = sha256(transfer.encode()).hexdigest() if transfer else secrets.token_hex(16)
        logical_id = prefix + suffix

        with self.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            query = self._scope(login, "nas_download", item.path).where(AccessLog.outcome == "allowed")
            previous = None
            if transfer:
                previous = session.scalar(query.where(AccessLog.logical_id == logical_id))
            elif resume:
                # 保留旧下载链接的续传能力。同一设备无标识的 Range 只能按最近一次下载归并。
                previous = session.scalar(query.where(AccessLog.logical_id.startswith(prefix))
                                          .order_by(AccessLog.id.desc()).limit(1))
            if previous is not None:
                return previous.id
            return self._create(session, login, "nas_download", host, item.path, logical_id, "allowed", "sending").id

    def delivered(self, record_id: int, size: int, offset: int, sent_bytes: int, complete: bool) -> None:
        with self.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(AccessLog, record_id)
            if row is None or row.delivery == "sent":
                return
            ranges = row.byte_ranges or []
            if sent_bytes:
                ranges = [*ranges, [offset, min(size, offset + sent_bytes)]]
            merged: list[list[int]] = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            row.byte_ranges = merged
            if merged == [[0, size]] or (size == 0 and complete):
                row.delivery = "sent"
            else:
                row.delivery = "partial" if complete else "interrupted"
            row.delivery_at = utcnow()
