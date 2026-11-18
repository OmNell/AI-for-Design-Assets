"""隔离登录会话，并为旧会话和需要本人连接的任务保留历史记录。"""
from alembic import op
import sqlalchemy as sa

revision = "0011_p0_device_sessions"
down_revision = "0004_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")
    op.execute("ALTER TABLE batches ADD COLUMN upload_session_id INTEGER REFERENCES sessions(id)")
    op.execute("ALTER TABLE jobs ADD COLUMN run_as_session_id INTEGER REFERENCES sessions(id)")


def downgrade() -> None:
    raise RuntimeError("请从升级前的备份恢复，不能重新启用旧会话")
