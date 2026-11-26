"""正式资产物理定位、待巡检及逻辑访问记录。"""
from alembic import op
import sqlalchemy as sa

revision = "0017_asset_access"
down_revision = "0016_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("physical_rel_path", sa.String))
    op.create_table("inspection_findings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Integer), sa.Column("kind", sa.String, nullable=False),
        sa.Column("path", sa.String, nullable=False), sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("files.id"), nullable=False),
        sa.Column("auto_fixed", sa.Boolean, nullable=False), sa.Column("detail", sa.Text, nullable=False),
        sa.UniqueConstraint("kind", "file_id", "path"))
    op.create_table("access_logs",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False), sa.Column("target_id", sa.Integer, nullable=False),
        sa.Column("logical_id", sa.String, nullable=False), sa.Column("outcome", sa.String, nullable=False),
        sa.Column("client_host", sa.String, nullable=False), sa.Column("delivery", sa.String),
        sa.Column("delivery_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "kind", "target_id", "logical_id"))
    op.create_index("ix_access_session_file", "access_logs", ["session_id", "kind", "target_id", "ts"])


def downgrade() -> None:
    raise RuntimeError("降级会丢失物理定位、异常及访问记录，请从升级前的备份恢复")
