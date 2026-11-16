"""卡片柜：文件、历史版本、商品、关系、标签值与永久链接。"""
from alembic import op
import sqlalchemy as sa

revision = "0007_catalog"
down_revision = "0006_audit"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.add_column("batch_products", sa.Column("product_code", sa.String))
    op.add_column("projects", sa.Column("archive_updated_at", sa.DateTime(timezone=True)))
    op.create_table("files", *common(),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False), sa.Column("rel_path", sa.String, nullable=False),
        sa.Column("size", sa.Integer, nullable=False), sa.Column("mtime", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.String, nullable=False), sa.Column("fingerprint_kind", sa.String, nullable=False),
        sa.Column("category", sa.String, nullable=False), sa.Column("status", sa.String, nullable=False),
        sa.Column("first_batch_id", sa.Integer, sa.ForeignKey("batches.id")))
    op.create_index("ix_files_project_id", "files", ["project_id"])
    op.create_table("products", *common(),
        sa.Column("product_code", sa.String, nullable=False, unique=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False), sa.Column("display_name", sa.String, nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("files.id"), nullable=False),
        sa.Column("archive_rel_path", sa.String, nullable=False), sa.Column("status", sa.String, nullable=False),
        sa.Column("free_text", sa.Text, nullable=False), sa.Column("vision_model", sa.String, nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_products_project_id", "products", ["project_id"])
    op.create_table("product_relations", *common(),
        sa.Column("set_product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
        sa.Column("item_product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
        sa.Column("source", sa.String, nullable=False), sa.Column("confirmed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("set_product_id", "item_product_id"))
    op.create_table("product_tags", *common(),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False), sa.Column("field_key", sa.String, nullable=False),
        sa.Column("value", sa.Text, nullable=False), sa.Column("terms", sa.JSON, nullable=False),
        sa.Column("source", sa.String, nullable=False), sa.Column("ai_value", sa.Text, nullable=False), sa.Column("flag", sa.String),
        sa.UniqueConstraint("product_id", "field_key"))
    op.create_index("ix_product_tags_field_key", "product_tags", ["field_key"])
    op.create_table("history_versions", *common(),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("files.id"), nullable=False),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id")),
        sa.Column("old_fingerprint", sa.String, nullable=False), sa.Column("old_rel_path", sa.String, nullable=False),
        sa.Column("new_rel_path", sa.String, nullable=False), sa.Column("reason", sa.String, nullable=False),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id")), sa.Column("expected_missing", sa.Boolean, nullable=False))
    op.create_table("links", *common(), sa.Column("link_id", sa.String, nullable=False, unique=True),
        sa.Column("kind", sa.String, nullable=False), sa.Column("target_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("kind", "target_id"))


def downgrade() -> None:
    for table in ("links", "history_versions", "product_tags", "product_relations", "products", "files"):
        op.drop_table(table)
    op.drop_column("projects", "archive_updated_at")
    op.drop_column("batch_products", "product_code")
