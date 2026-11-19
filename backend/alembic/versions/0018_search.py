"""检索缓存、查询及点击审计、向量索引代次。保留 WP09 向量及正式指针。"""
from alembic import op
import sqlalchemy as sa

revision = "0018_search"
down_revision = "0017_asset_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("query_cache", sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("conditions", sa.JSON, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("search_logs", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False), sa.Column("query", sa.Text, nullable=False),
        sa.Column("parsed", sa.JSON, nullable=False), sa.Column("result_count", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False), sa.Column("index_generation", sa.Integer),
        sa.Column("ai_usage", sa.JSON, nullable=False), sa.Column("warnings", sa.JSON, nullable=False),
        sa.Column("targets", sa.JSON, nullable=False))
    op.create_table("search_clicks", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("search_id", sa.Integer, sa.ForeignKey("search_logs.id"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_kind", sa.String, nullable=False), sa.Column("target_id", sa.Integer, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False))
    op.create_table("embedding_indexes", sa.Column("generation", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False), sa.Column("version", sa.String, nullable=False),
        sa.Column("revision", sa.String, nullable=False), sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.execute("CREATE UNIQUE INDEX uq_embedding_index_candidate ON embedding_indexes(status) WHERE status='candidate'")
    op.create_index("ix_embeddings_generation", "product_embeddings", ["index_generation", "model_id", "model_version", "state"])


def downgrade() -> None:
    raise RuntimeError("请保留入库向量和已切换的模型指针，从升级前备份恢复")
