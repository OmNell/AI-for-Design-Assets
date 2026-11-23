"""审核回避和单账号首次入库例外。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ReviewConfig
from app.db.models import Batch, ReviewSnapshot, User


def self_review_allowed(session: Session, config: ReviewConfig, batch: Batch, user: User | None) -> bool:
    if (user is None or not config.self_review_admin or user.username != config.self_review_admin
            or not user.enabled or "admin" not in user.roles or batch.kind != "ingest"
            or batch.uploader_id != user.id):
        return False
    submitter = session.scalar(select(ReviewSnapshot.submitted_by).where(
        ReviewSnapshot.batch_id == batch.id, ReviewSnapshot.round == batch.round))
    return submitter == user.id


def recused(session: Session, config: ReviewConfig, batch: Batch, user: User) -> bool:
    if self_review_allowed(session, config, batch, user):
        return False
    return user.id == batch.uploader_id or session.scalar(select(ReviewSnapshot.id).where(
        ReviewSnapshot.batch_id == batch.id, ReviewSnapshot.submitted_by == user.id).limit(1)) is not None or (
        batch.status == "待终审" and batch.first_reviewer_id == user.id)
