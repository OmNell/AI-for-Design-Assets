"""统一通知入口：第一版只有站内待办（由批次状态实时汇总），钉钉接口预留为空实现。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import AiAlert, Batch, InspectionFinding, InspectionRun, Job, User, utcnow
from app.errors import AppError
from app.schemas.audit import TodoBatch, TodoItem
from app.services.admin.company_map import batch_company, match_company
from app.services.ai.orchestrator import PROCESSING, ai_run_stalled
from app.services.ai.settings import AiSettingsService
from app.services.auth.service import AuthService
from app.services.review.policy import recused

LOCAL = timezone(timedelta(hours=8))


def review_overdue(start: datetime, now: datetime, workdays: int) -> bool:
    """以进入本阶段的本地钟点为界，按周一至周五推算期限。"""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    deadline = start.astimezone(LOCAL)
    while workdays > 0:
        deadline += timedelta(days=1)
        if deadline.weekday() < 5:
            workdays -= 1
    return now > deadline


def stage_since(batch: Batch) -> datetime:
    raw = batch.payload.get("stage_since")
    value = datetime.fromisoformat(raw) if isinstance(raw, str) else batch.updated_at
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotifyService:
    def __init__(self, auth: AuthService, ai_settings: AiSettingsService) -> None:
        self.auth = auth
        self.ai_settings = ai_settings

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
                ["待核对", "已驳回", "待初审", "待终审", "AI 处理失败", "入库中", "入库异常"])).order_by(Batch.id)))
            items: list[TodoItem] = []

            def add(key: str, label: str, rows: list[Batch], link: str) -> None:
                if rows:
                    items.append(TodoItem(key=key, label=label, count=len(rows), link=link,
                        batches=[TodoBatch(id=row.id, label=str(row.payload.get("folder_name", row.batch_no))) for row in rows]))

            def single(rows: list[Batch], pattern: str) -> str:
                return pattern.format(id=rows[0].id) if len(rows) == 1 else "/"

            mine = [row for row in batches if row.owner_id == user.id]
            checking = [row for row in mine if row.status == "待核对"]
            rejected = [row for row in mine if row.status == "已驳回"]
            add("check", "待核对", checking, single(checking, "/batches/{id}/review") if checking else "/")
            add("rejected", "已驳回", rejected, single(rejected, "/batches/{id}/review") if rejected else "/")
            if "first_reviewer" in roles:
                add("first_review", "待初审", [row for row in batches if row.status == "待初审" and row.first_reviewer_id in (None, user.id) and not recused(row, user)], "/first-review")
            if "final_reviewer" in roles:
                add("final_review", "待终审", [row for row in batches if row.status == "待终审" and row.final_reviewer_id in (None, user.id) and not recused(row, user)], "/final-review")
            failed = [row for row in batches if row.status == "AI 处理失败" and ("admin" in roles or row.owner_id == user.id)]
            add("ai_failed", "AI 处理失败", failed, single(failed, "/batches/{id}") if failed else "/")
            stalled_query = (select(Batch)
                .join(AiAlert, (AiAlert.batch_id == Batch.id) & (AiAlert.run_id == Batch.payload["ai_run"].as_string()))
                .where(Batch.status == PROCESSING).order_by(Batch.id))
            if "admin" not in roles:
                stalled_query = stalled_query.where(Batch.owner_id == user.id)
            alarmed = list(session.scalars(stalled_query))
            if alarmed:
                minutes = self.ai_settings._effective(session).stall_alert_min
                stalled = [row for row in alarmed if ai_run_stalled(row, now, minutes)]
                add("ai_stalled", "AI 处理超时", stalled, single(stalled, "/batches/{id}"))

            if "admin" in roles:
                waiting = [row for row in batches if row.status in ("待初审", "待终审")
                           and match_company(session, batch_company(row)) is None]
                add("path_pending", "位置待定，等待对照表录入", waiting, "/admin/company-map")
                limit = self.auth.config.review.overdue_days
                overdue = [row for row in batches if row.status in ("待初审", "待终审")
                           and review_overdue(stage_since(row), now, limit)]
                add("overdue", f"待审超过 {limit} 个工作日", overdue, single(overdue, "/review/{id}") if overdue else "/")
            unfinished = [row for row in batches if row.status in ("入库中", "入库异常") and (
                "admin" in roles or ("final_reviewer" in roles and row.final_reviewer_id == user.id))]
            add("ingest", "入库未完成", unfinished, single(unfinished, "/review/{id}"))

            recipients = [Batch.uploader_id == user.id]
            if roles & {"first_reviewer", "admin"}:
                recipients.append(Batch.first_reviewer_id == user.id)
            if roles & {"final_reviewer", "admin"}:
                recipients.append(Batch.final_reviewer_id == user.id)
            completed = list(session.scalars(select(Batch).where(Batch.status == "已入库",
                Batch.kind.in_(("ingest", "supplement")), or_(*recipients)).order_by(Batch.updated_at.desc(), Batch.id.desc())))
            add("ingested", "入库完成", completed, single(completed, "/batches/{id}"))

            if "admin" in roles:
                findings = session.scalar(select(func.count(InspectionFinding.id)).where(
                    InspectionFinding.acknowledged_at.is_(None), InspectionFinding.resolved_at.is_(None)))
                if findings:
                    items.append(TodoItem(key="asset_findings", label="正式文件异常待处理", count=findings,
                                          link="/admin/maintenance?tab=inspection", batches=[]))
                latest = session.scalar(select(InspectionRun).order_by(InspectionRun.id.desc()).limit(1))
                if latest and latest.status == "done":
                    items.append(TodoItem(key="inspection_report", label=f"最近巡检报告 · {latest.summary.get('count', 0)} 项异常",
                                          count=1, link="/admin/maintenance", batches=[]))
                failed = session.scalar(select(func.count(Job.id)).where(
                    Job.kind.in_(("inspection", "backup")), Job.status == "failed"))
                if failed:
                    items.append(TodoItem(key="maintenance_failures", label="巡检或备份任务异常", count=failed,
                                          link="/admin/maintenance", batches=[]))
            if "final_reviewer" in roles:
                repairs = session.scalar(select(func.count(func.distinct(InspectionFinding.project_id))).where(
                    InspectionFinding.kind == "archive_broken", InspectionFinding.resolved_at.is_(None)))
                if repairs:
                    items.append(TodoItem(key="archive_repairs", label="项目档案待修复", count=repairs, link="/archive/repairs", batches=[]))
            return items
