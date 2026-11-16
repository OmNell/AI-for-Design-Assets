"""分片上传的项目、批次、清单与预检确认表。"""
from alembic import op
import sqlalchemy as sa

revision = "0002_upload"
down_revision = "0001_skeleton"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("projects", *common(),
        sa.Column("project_code", sa.String, nullable=False, unique=True), sa.Column("code_source", sa.String, nullable=False),
        sa.Column("company_name", sa.String, nullable=False), sa.Column("company_id", sa.Integer), sa.Column("industry_id", sa.Integer),
        sa.Column("won", sa.Boolean, nullable=False), sa.Column("project_name", sa.String, nullable=False),
        sa.Column("folder_name", sa.String, nullable=False), sa.Column("current_path", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False), sa.Column("ingested_at", sa.DateTime(timezone=True)))
    op.create_table("batches", *common(), sa.Column("batch_no", sa.String, nullable=False, unique=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")), sa.Column("project_code", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False), sa.Column("status", sa.String, nullable=False),
        sa.Column("owner_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("uploader_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("first_reviewer_id", sa.Integer, sa.ForeignKey("users.id")), sa.Column("final_reviewer_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("round", sa.Integer, nullable=False), sa.Column("temp_path", sa.String, nullable=False), sa.Column("won", sa.Boolean, nullable=False),
        sa.Column("recommended_path", sa.String), sa.Column("final_path", sa.String), sa.Column("note_to_reviewer", sa.Text),
        sa.Column("payload", sa.JSON, nullable=False), sa.Column("ingest_step", sa.Integer, nullable=False), sa.Column("ingest_error", sa.Text))
    op.create_table("batch_files", *common(), sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("rel_path", sa.String, nullable=False), sa.Column("size", sa.Integer, nullable=False), sa.Column("mtime", sa.Integer, nullable=False),
        sa.Column("upload_status", sa.String, nullable=False), sa.Column("bytes_received", sa.Integer, nullable=False), sa.Column("fingerprint", sa.String),
        sa.Column("conflict", sa.String), sa.Column("conflict_choice", sa.String), sa.Column("removed", sa.Boolean, nullable=False),
        sa.UniqueConstraint("batch_id", "rel_path"))
    op.create_index("ix_batch_files_batch_id", "batch_files", ["batch_id"])
    op.create_table("preflight_acks", *common(), sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("rule_code", sa.String, nullable=False), sa.UniqueConstraint("batch_id", "rule_code"))


def downgrade() -> None:
    for table in ("preflight_acks", "batch_files", "batches", "projects"):
        op.drop_table(table)
