"""为实时 NAS 文件记录相对路径与已发送字节区间。"""
from alembic import op
import sqlalchemy as sa

revision = "0023_nas_access_audit"
down_revision = "0022_dual_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("access_logs", sa.Column("target_path", sa.String()))
    op.add_column("access_logs", sa.Column("byte_ranges", sa.JSON()))


def downgrade() -> None:
    raise RuntimeError("降级会丢失 NAS 访问记录的路径和传输信息，请从升级前的备份恢复")
