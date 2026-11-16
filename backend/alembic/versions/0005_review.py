"""核对阶段记录自由描述的来源与 AI 原值。"""
from alembic import op
import sqlalchemy as sa

revision = "0005_review"
down_revision = "0004_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("batch_products", sa.Column("free_text_source", sa.String, nullable=False, server_default="ai"))
    op.add_column("batch_products", sa.Column("ai_free_text", sa.Text, nullable=False, server_default=""))
    # 既有描述全部来自模型，原样作为 AI 原值。
    op.execute("UPDATE batch_products SET ai_free_text = free_text")


def downgrade() -> None:
    op.drop_column("batch_products", "ai_free_text")
    op.drop_column("batch_products", "free_text_source")
