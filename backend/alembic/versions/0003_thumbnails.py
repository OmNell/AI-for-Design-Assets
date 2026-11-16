"""记录文件缩略图任务结果，不改变上传完成状态。"""
from alembic import op
import sqlalchemy as sa

revision = "0003_thumbnails"
down_revision = "0002_upload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("batch_files", sa.Column("thumb_status", sa.String, nullable=True))
    op.add_column("batch_files", sa.Column("thumb_error", sa.Text, nullable=True))
    op.create_index("ix_jobs_status_scheduled_at", "jobs", ["status", "scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_scheduled_at", table_name="jobs")
    op.drop_column("batch_files", "thumb_error")
    op.drop_column("batch_files", "thumb_status")
