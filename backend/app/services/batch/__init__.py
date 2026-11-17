"""批次的公共规则：类型名称、同一项目只允许一个未完成的操作批次。"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Batch, Project

KIND_LABELS = {"ingest": "首次入库", "supplement": "补充", "rename": "改名", "retag": "修改标签", "void": "作废", "move": "移动", "purge": "物理删除"}
# 改名、修改标签不动文件内容，可配置为只需终审一级。
LIGHT_KINDS = ("rename", "retag")
FINISHED = ("已入库", "已撤回")
# 执行阶段沿用批次状态机里的名字：入库中 / 入库异常 / 已入库。
RUNNING, BROKEN, DONE, WITHDRAWN = "入库中", "入库异常", "已入库", "已撤回"


def kind_label(batch: Batch) -> str:
    if batch.kind == "void":
        params = batch.payload.get("params", {}) if isinstance(batch.payload.get("params"), dict) else {}
        action = "恢复" if params.get("action") == "restore" else "作废"
        return ("整项目" + action) if params.get("scope") == "project" else action
    return KIND_LABELS.get(batch.kind, batch.kind)


def open_batch(session: Session, project: Project, exclude: int | None = None) -> Batch | None:
    """该项目尚未结束的批次（首次入库已完成的不算）。"""
    query = select(Batch).where(or_(Batch.project_id == project.id, Batch.project_code == project.project_code),
                                Batch.status.not_in(FINISHED)).order_by(Batch.id)
    return next((batch for batch in session.scalars(query) if batch.id != exclude), None)
