"""自由文本标签字段、批次商品、企业对照与 AI 调用记录。"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0004_ai"
down_revision = "0003_thumbnails"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("tag_fields", *common(), sa.Column("key", sa.String, nullable=False, unique=True),
        sa.Column("label", sa.String, nullable=False), sa.Column("hint", sa.Text, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False), sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False))
    op.create_table("batch_products", *common(),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("batch_file_id", sa.Integer, sa.ForeignKey("batch_files.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False), sa.Column("display_name", sa.String, nullable=False),
        sa.Column("existing_product_id", sa.Integer), sa.Column("ai_status", sa.String, nullable=False),
        sa.Column("ai_error", sa.Text), sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("free_text", sa.Text, nullable=False), sa.Column("relation_items", sa.JSON, nullable=False),
        sa.Column("relation_confirmed", sa.Boolean, nullable=False), sa.Column("flags", sa.JSON, nullable=False),
        sa.UniqueConstraint("batch_id", "batch_file_id"))
    op.create_index("ix_batch_products_batch_id", "batch_products", ["batch_id"])
    op.create_table("company_industry_map", *common(),
        sa.Column("company_name", sa.String, nullable=False, unique=True), sa.Column("aliases", sa.JSON, nullable=False),
        sa.Column("company_seq", sa.Integer, nullable=False), sa.Column("industry_name", sa.String, nullable=False),
        sa.Column("industry_seq", sa.Integer, nullable=False))
    op.create_table("ai_calls", *common(), sa.Column("model", sa.String, nullable=False), sa.Column("kind", sa.String, nullable=False),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id")),
        sa.Column("batch_product_id", sa.Integer, sa.ForeignKey("batch_products.id")),
        sa.Column("duration_ms", sa.Integer, nullable=False), sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False), sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("success", sa.Boolean, nullable=False), sa.Column("error", sa.Text))
    op.create_index("ix_ai_calls_batch_id", "ai_calls", ["batch_id"])
    op.create_table("ai_alerts", *common(), sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("run_id", sa.String, nullable=False), sa.Column("reason", sa.Text, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("batch_id", "run_id"))
    fields = sa.table("tag_fields", sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
        sa.column("key", sa.String), sa.column("label", sa.String), sa.column("hint", sa.Text),
        sa.column("sort_order", sa.Integer), sa.column("enabled", sa.Boolean), sa.column("version", sa.Integer))
    # 填写说明逐字取自《入库流程》9.5，不维护取值、必填或数量限制。
    initial = [
        ("category", "品类", "这是什么东西"), ("material", "材质", "主要材料"),
        ("color", "主色", "主要颜色"), ("style", "风格", "设计风格"),
        ("festival", "节日场景", "适用的具体节日；不针对节日就写「通用」"),
        ("audience", "受众群体", "给谁用、送给谁"), ("usage_scene", "使用场景", "用途和使用的场合"),
        ("size", "尺寸", "大小的描述"), ("craft", "工艺", "制作或装饰工艺"),
        ("material_name", "物料名称", "物料的称呼"), ("structure", "结构", "组成部分和形态结构"),
    ]
    now = datetime.now(timezone.utc)
    op.bulk_insert(fields, [{"key": key, "label": label, "hint": hint, "sort_order": index, "enabled": True,
                           "version": 1, "created_at": now, "updated_at": now}
                          for index, (key, label, hint) in enumerate(initial)])


def downgrade() -> None:
    for table in ("ai_alerts", "ai_calls", "company_industry_map", "batch_products", "tag_fields"):
        op.drop_table(table)
