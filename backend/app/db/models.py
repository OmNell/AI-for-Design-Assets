"""工作台已落地的业务表，时间统一使用 UTC。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Record, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str | None] = mapped_column(String)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Record, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    client_host: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Setting(Record, Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, unique=True)
    value: Mapped[object] = mapped_column(JSON)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)


class Oplog(Record, Base):
    __tablename__ = "oplog"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str | None] = mapped_column(String)
    before: Mapped[object | None] = mapped_column(JSON(none_as_null=True))
    after: Mapped[object | None] = mapped_column(JSON(none_as_null=True))
    result: Mapped[str] = mapped_column(String)
    client_host: Mapped[str] = mapped_column(String)


class Job(Record, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_scheduled_at", "status", "scheduled_at"),)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[object] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_as_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Record, Base):
    __tablename__ = "projects"
    project_code: Mapped[str] = mapped_column(String, unique=True)
    code_source: Mapped[str] = mapped_column(String, default="company")
    company_name: Mapped[str] = mapped_column(String)
    company_id: Mapped[int | None] = mapped_column(Integer)
    industry_id: Mapped[int | None] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column(Boolean)
    project_name: Mapped[str] = mapped_column(String)
    folder_name: Mapped[str] = mapped_column(String)
    current_path: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="active")
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Batch(Record, Base):
    __tablename__ = "batches"
    batch_no: Mapped[str] = mapped_column(String, unique=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project_code: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    first_reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    final_reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    round: Mapped[int] = mapped_column(Integer, default=1)
    temp_path: Mapped[str] = mapped_column(String)
    won: Mapped[bool] = mapped_column(Boolean)
    recommended_path: Mapped[str | None] = mapped_column(String)
    final_path: Mapped[str | None] = mapped_column(String)
    note_to_reviewer: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    ingest_step: Mapped[int] = mapped_column(Integer, default=0)
    ingest_error: Mapped[str | None] = mapped_column(Text)


class BatchFile(Record, Base):
    __tablename__ = "batch_files"
    __table_args__ = (UniqueConstraint("batch_id", "rel_path"),)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    rel_path: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[int] = mapped_column(Integer)
    upload_status: Mapped[str] = mapped_column(String, default="pending")
    bytes_received: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str | None] = mapped_column(String)
    thumb_status: Mapped[str | None] = mapped_column(String)
    thumb_error: Mapped[str | None] = mapped_column(Text)
    conflict: Mapped[str | None] = mapped_column(String)
    conflict_choice: Mapped[str | None] = mapped_column(String)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


class PreflightAck(Record, Base):
    __tablename__ = "preflight_acks"
    __table_args__ = (UniqueConstraint("batch_id", "rule_code"),)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    rule_code: Mapped[str] = mapped_column(String)


class TagField(Record, Base):
    __tablename__ = "tag_fields"
    key: Mapped[str] = mapped_column(String, unique=True)
    label: Mapped[str] = mapped_column(String)
    hint: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class BatchProduct(Record, Base):
    __tablename__ = "batch_products"
    __table_args__ = (UniqueConstraint("batch_id", "batch_file_id"),)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    batch_file_id: Mapped[int] = mapped_column(ForeignKey("batch_files.id"))
    kind: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    existing_product_id: Mapped[int | None] = mapped_column(Integer)
    ai_status: Mapped[str] = mapped_column(String, default="pending")
    ai_error: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    free_text: Mapped[str] = mapped_column(Text, default="")
    relation_items: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    relation_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)


class CompanyIndustryMap(Record, Base):
    __tablename__ = "company_industry_map"
    company_name: Mapped[str] = mapped_column(String, unique=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    company_seq: Mapped[int] = mapped_column(Integer)
    industry_name: Mapped[str] = mapped_column(String)
    industry_seq: Mapped[int] = mapped_column(Integer)


class AiCall(Record, Base):
    __tablename__ = "ai_calls"
    model: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), index=True)
    batch_product_id: Mapped[int | None] = mapped_column(ForeignKey("batch_products.id"))
    duration_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)


class AiAlert(Record, Base):
    __tablename__ = "ai_alerts"
    __table_args__ = (UniqueConstraint("batch_id", "run_id"),)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    run_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
