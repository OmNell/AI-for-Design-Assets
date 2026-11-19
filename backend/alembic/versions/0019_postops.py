"""入库后版本、资产生命周期、向量历史和预期删除。"""
from alembic import op
import sqlalchemy as sa

revision = "0019_postops"
down_revision = "0018_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("projects", "products", "files"):
        for name, type_ in (
            ("voided_at", sa.DateTime(timezone=True)), ("void_reason", sa.Text()),
            ("void_batch_id", sa.Integer()), ("deleted_at", sa.DateTime(timezone=True)),
            ("delete_reason", sa.Text()),
        ):
            op.add_column(table, sa.Column(name, type_))
    op.add_column("projects", sa.Column("asset_version", sa.Integer, nullable=False, server_default="0"))
    op.add_column("projects", sa.Column("canonical_path", sa.String))
    op.add_column("projects", sa.Column("restore_path", sa.String))
    op.add_column("files", sa.Column("void_prior_status", sa.String))
    op.create_table(
        "postop_plans",
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), primary_key=True),
        sa.Column("previous_path", sa.String, nullable=False),
        sa.Column("target_path", sa.String, nullable=False),
        *(sa.Column(name, sa.JSON, nullable=False) for name in ("baseline", "manifest", "sources", "purge", "progress")),
    )
    op.create_table(
        "product_embedding_history",
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), primary_key=True),
        sa.Column("model_id", sa.String, primary_key=True),
        sa.Column("model_version", sa.String, primary_key=True),
        sa.Column("index_generation", sa.Integer, primary_key=True),
        sa.Column("dimension", sa.Integer, nullable=False),
        sa.Column("embedding", sa.LargeBinary, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "expected_deletions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("files.id"), nullable=False),
        sa.Column("path", sa.String, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("intent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("batch_id", "file_id", "path"),
    )
    op.create_index("ix_expected_deletions_batch_id", "expected_deletions", ["batch_id"])


def downgrade() -> None:
    raise RuntimeError("入库后资产版本及预期删除记录必须保留，请从升级前备份恢复")
