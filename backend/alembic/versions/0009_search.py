"""检索：商品向量、搜索日志与查询理解缓存。"""
from alembic import op
import sqlalchemy as sa

revision = "0009_search"
down_revision = "0008_findings"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    # 向量以 BLOB 存在普通表里，是两种向量实现共同的数据来源；sqlite-vec 的虚拟表在运行时按需建立。
    op.create_table("product_embeddings", *common(),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False, unique=True),
        sa.Column("text_hash", sa.String, nullable=False), sa.Column("model", sa.String, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False), sa.Column("embedding", sa.LargeBinary, nullable=False))
    op.create_table("search_logs", *common(), sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("query", sa.Text, nullable=False), sa.Column("kind", sa.String, nullable=False), sa.Column("conditions", sa.JSON),
        sa.Column("result_count", sa.Integer, nullable=False), sa.Column("elapsed_ms", sa.Integer, nullable=False),
        sa.Column("local_ms", sa.Integer, nullable=False), sa.Column("model_calls", sa.Integer, nullable=False),
        sa.Column("degraded", sa.Boolean, nullable=False), sa.Column("clicks", sa.JSON, nullable=False))
    op.create_index("ix_search_logs_created_at", "search_logs", ["created_at"])
    op.create_table("query_cache", *common(), sa.Column("query_hash", sa.String, nullable=False),
        sa.Column("fields_version", sa.Integer, nullable=False), sa.Column("query", sa.Text, nullable=False),
        sa.Column("result", sa.JSON, nullable=False), sa.UniqueConstraint("query_hash", "fields_version"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vec_products")
    for table in ("query_cache", "search_logs", "product_embeddings"):
        op.drop_table(table)
