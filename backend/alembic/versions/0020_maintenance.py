"""巡检报告、备份校验及个人身份临时区清理清单。"""
from alembic import op
import sqlalchemy as sa

revision = "0020_maintenance"
down_revision = "0019_postops"
branch_labels = None
depends_on = None


def times() -> tuple[sa.Column, sa.Column]:
    return tuple(sa.Column(name, sa.DateTime(timezone=True), nullable=False) for name in ("created_at", "updated_at"))


def upgrade() -> None:
    op.add_column("files", sa.Column("inspection_probe", sa.JSON))
    op.create_table("inspection_runs", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String, nullable=False), sa.Column("summary", sa.JSON, nullable=False),
        sa.Column("error", sa.Text), *times())
    with op.batch_alter_table("inspection_findings", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.alter_column("project_id", existing_type=sa.Integer, nullable=True)
        batch.alter_column("file_id", existing_type=sa.Integer, nullable=True)
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("acknowledged_by", sa.Integer, sa.ForeignKey("users.id", name="fk_finding_ack_user")))
        batch.drop_constraint("uq_inspection_findings_kind", type_="unique")
        batch.create_unique_constraint("uq_findings_run_path", ["kind", "file_id", "path", "run_id"])
        batch.create_index("ix_inspection_findings_run_id", ["run_id"])
    op.create_table("backup_records", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False, unique=True), sa.Column("size", sa.Integer),
        sa.Column("fingerprint", sa.String(64)), sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("local_retained", sa.Boolean, nullable=False), *times())
    op.create_table("cleanup_plans", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id")), sa.Column("target", sa.String, nullable=False),
        sa.Column("entries", sa.JSON, nullable=False), sa.Column("confirmed_at", sa.DateTime(timezone=True)), *times())
    op.execute("CREATE UNIQUE INDEX uq_maintenance_job_active ON jobs(kind) "
               "WHERE kind IN ('inspection','backup') AND status IN ('queued','running') "
               "AND json_extract(payload, '$.exclusive') = 1")


def downgrade() -> None:
    raise RuntimeError("请从升级前备份恢复，保留维护审计记录")
