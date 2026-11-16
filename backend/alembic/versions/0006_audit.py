"""两级审核记录、钉图意见与行业清单。"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0006_audit"
down_revision = "0005_review"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("industries", *common(), sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("seq", sa.Integer, nullable=False, unique=True))
    op.create_table("review_rounds", *common(),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("round", sa.Integer, nullable=False), sa.Column("stage", sa.String, nullable=False),
        sa.Column("reviewer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.String), sa.Column("comment", sa.Text), sa.Column("checklist", sa.JSON),
        sa.Column("decided_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("batch_id", "round", "stage"))
    op.create_index("ix_review_rounds_batch_id", "review_rounds", ["batch_id"])
    op.create_table("review_pins", *common(),
        sa.Column("review_round_id", sa.Integer, sa.ForeignKey("review_rounds.id"), nullable=False),
        sa.Column("batch_product_id", sa.Integer, sa.ForeignKey("batch_products.id"), nullable=False),
        sa.Column("x", sa.Float, nullable=False), sa.Column("y", sa.Float, nullable=False), sa.Column("text", sa.Text, nullable=False))
    op.create_index("ix_review_pins_review_round_id", "review_pins", ["review_round_id"])
    # 行业原先只附着在企业行上；抽出独立清单后才能先建行业、再录企业。
    rows = op.get_bind().execute(sa.text("SELECT DISTINCT industry_name, industry_seq FROM company_industry_map ORDER BY industry_seq")).fetchall()
    now = datetime.now(timezone.utc)
    table = sa.table("industries", sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
                     sa.column("name", sa.String), sa.column("seq", sa.Integer))
    seen: set[object] = set()
    values = []
    for name, seq in rows:
        if name in seen or seq in {value["seq"] for value in values}:
            continue
        seen.add(name)
        values.append({"name": name, "seq": seq, "created_at": now, "updated_at": now})
    if values:
        op.bulk_insert(table, values)


def downgrade() -> None:
    for table in ("review_pins", "review_rounds", "industries"):
        op.drop_table(table)
