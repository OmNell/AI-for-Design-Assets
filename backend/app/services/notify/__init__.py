"""统一通知入口：第一版只有站内待办（由批次状态实时汇总），钉钉接口预留为空实现。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Batch, User, utcnow
from app.errors import AppError
from app.schemas.audit import TodoBatch, TodoItem
from app.services.admin.company_map import batch_company, match_company
from app.services.auth.service import AuthService

LOCAL = timezone(timedelta(hours=8))
logger = logging.getLogger("nas_workbench")


def business_days_elapsed(start: datetime, now: datetime) -> int:
    """从 start 到 now 已满的工作日数（周一至周五，不含法定节假日调休）。"""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    begin, end = start.astimezone(LOCAL), now.astimezone(LOCAL)
    days, cursor = 0, begin.date()
    while cursor < end.date():
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    # 当天还没到提交时的钟点，最后一天不算满。
    if days and end.weekday() < 5 and end.time() < begin.time():
        days -= 1
    return days


def stage_since(batch: Batch) -> datetime:
    raw = batch.payload.get("stage_since")
    value = datetime.fromisoformat(raw) if isinstance(raw, str) else batch.updated_at
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotifyService:
    def __init__(self, auth: AuthService) -> None:
        self.auth = auth

    def emit(self, session: Session, event: str, batch: Batch) -> None:
        """业务事件的统一出口。站内待办不落表，由 todos() 按状态汇总；这里只负责外发渠道。"""
        self.send_dingtalk(event, batch.id)

    def send_dingtalk(self, event: str, batch_id: int) -> None:
        """钉钉发送预留：第一版不外发任何消息。"""
        return None

    def todos(self, actor: User, now: datetime | None = None) -> list[TodoItem]:
        now = now or utcnow()
        with self.auth.lock, self.auth.database.session() as session:
            user = session.get(User, actor.id)
            if user is None or not user.enabled:
                raise AppError("forbidden", "当前账号没有此操作权限", 403)
            roles = set(user.roles)
            batches = list(session.scalars(select(Batch).where(Batch.status.in_(
                ["待核对", "已驳回", "待初审", "待终审", "AI 处理失败", "入库异常"])).order_by(Batch.id)))
            items: list[TodoItem] = []

            def add(key: str, label: str, rows: list[Batch], link: str) -> None:
                if rows:
                    items.append(TodoItem(key=key, label=label, count=len(rows), link=link,
                        batches=[TodoBatch(id=row.id, label=str(row.payload.get("folder_name", row.batch_no))) for row in rows[:20]]))

            def single(rows: list[Batch], pattern: str) -> str:
                return pattern.format(id=rows[0].id) if len(rows) == 1 else "/"

            mine = [row for row in batches if row.owner_id == user.id]
            checking = [row for row in mine if row.status == "待核对"]
            rejected = [row for row in mine if row.status == "已驳回"]
            add("check", "待核对", checking, single(checking, "/batches/{id}/review") if checking else "/")
            add("rejected", "已驳回", rejected, single(rejected, "/batches/{id}/review") if rejected else "/")
            if "first_reviewer" in roles:
                add("first_review", "待初审", [row for row in batches if row.status == "待初审"], "/first-review")
            if "final_reviewer" in roles:
                add("final_review", "待终审", [row for row in batches if row.status == "待终审"], "/final-review")
            failed = [row for row in batches if row.status == "AI 处理失败" and ("admin" in roles or row.owner_id == user.id)]
            add("ai_failed", "AI 处理失败", failed, single(failed, "/batches/{id}") if failed else "/")
            broken = [row for row in batches if row.status == "入库异常" and row.final_reviewer_id == user.id and "final_reviewer" in roles]
            add("ingest_broken", "有批次入库未完成", broken, single(broken, "/review/{id}") if broken else "/")
            if "admin" in roles:
                failed_ingest = [row for row in batches if row.status == "入库异常"]
                add("ingest_alert", "入库异常（等终审继续）", failed_ingest, single(failed_ingest, "/review/{id}") if failed_ingest else "/")
                waiting = [row for row in batches if row.status in ("待初审", "待终审") and not (row.final_path or row.recommended_path)
                           and match_company(session, batch_company(row)) is None]
                add("path_pending", "位置待定，等待对照表录入", waiting, "/admin/company-map")
                limit = self.auth.config.review.overdue_days
                overdue = [row for row in batches if row.status in ("待初审", "待终审")
                           and business_days_elapsed(stage_since(row), now) >= limit]
                add("overdue", f"待审超过 {limit} 个工作日", overdue, single(overdue, "/review/{id}") if overdue else "/")
            return items
