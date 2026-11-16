"""待巡检记录：下载时找不到文件即登记，巡检工作包沿用同一张表。"""
from alembic import op
import sqlalchemy as sa

revision = "0008_findings"
down_revision = "0007_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("inspection_findings", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Integer), sa.Column("kind", sa.String, nullable=False), sa.Column("path", sa.String, nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")), sa.Column("file_id", sa.Integer, sa.ForeignKey("files.id")),
        sa.Column("auto_fixed", sa.Boolean, nullable=False), sa.Column("detail", sa.Text))
    op.create_index("ix_inspection_findings_file_id", "inspection_findings", ["file_id"])


def downgrade() -> None:
    op.drop_table("inspection_findings")
