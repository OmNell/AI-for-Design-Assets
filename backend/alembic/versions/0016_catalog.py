"""卡片柜：文件、历史版本、商品、关系、标签值与永久链接。"""
from alembic import op
import sqlalchemy as sa

revision = "0016_catalog"
down_revision = "0015_audit"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.add_column("batch_products", sa.Column("product_code", sa.String))
    op.create_index("uq_batch_products_code", "batch_products", ["product_code"], unique=True)
    op.add_column("projects", sa.Column("archive_updated_at", sa.DateTime(timezone=True)))
    op.add_column("projects", sa.Column("archive_review", sa.JSON))
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
        sa.Column("confirmed_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String, nullable=False, server_default="active"),
        sa.Column("confirmed_by_original_id", sa.Integer), sa.Column("inactive_reason", sa.String), sa.Column("relation_version", sa.Integer, nullable=False, server_default="1"), sa.UniqueConstraint("set_product_id", "item_product_id"))
    op.create_table("product_tags", *common(),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False), sa.Column("field_key", sa.String, nullable=False),
        sa.Column("value", sa.Text, nullable=False), sa.Column("terms", sa.JSON, nullable=False),
        sa.Column("source", sa.String, nullable=False), sa.Column("ai_value", sa.Text, nullable=False), sa.Column("flag", sa.String), sa.Column("confirmation_status", sa.String, nullable=False),
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

    op.create_table("ingest_plans", sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), primary_key=True),
        sa.Column("manifest", sa.JSON, nullable=False))
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER ingest_plan_locked_{action} BEFORE {action} ON ingest_plans
            BEGIN SELECT RAISE(ABORT, '预分配的入库身份不可更改'); END""")
    # 同一批次至多有一个可运行的入库任务，并保留过去的失败任务。
    op.execute("""CREATE UNIQUE INDEX uq_ingest_live_job ON jobs(json_extract(payload, '$.batch_id'))
        WHERE kind='ingest' AND status IN ('queued','running')""")
    op.create_table("product_embeddings",
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), primary_key=True),
        sa.Column("model_id", sa.String, primary_key=True), sa.Column("model_version", sa.String, primary_key=True),
        sa.Column("index_generation", sa.Integer, primary_key=True), sa.Column("dimension", sa.Integer),
        sa.Column("embedding", sa.LargeBinary), sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String, nullable=False), sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error", sa.Text))


def downgrade() -> None:
    raise RuntimeError("降级会丢失正式身份和永久链接，请从升级前的备份恢复")
