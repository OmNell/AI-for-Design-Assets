"""入库后操作：历史版本的快照，以及项目、商品、文件的作废与删除留痕。"""
from alembic import op
import sqlalchemy as sa

revision = "0010_postops"
down_revision = "0009_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("history_versions", sa.Column("snapshot", sa.JSON))
    for table in ("projects", "products", "files"):
        op.add_column(table, sa.Column("lifecycle", sa.JSON, nullable=False, server_default="{}"))


def downgrade() -> None:
    for table in ("projects", "products", "files"):
        op.drop_column(table, "lifecycle")
    op.drop_column("history_versions", "snapshot")
