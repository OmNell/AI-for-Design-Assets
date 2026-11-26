"""两级审核记录、钉图意见与行业清单。"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0015_audit"
down_revision = "0014_review_snapshots"
branch_labels = None
depends_on = None


def common() -> list[sa.Column]:
    return [sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table("industries", *common(), sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("last_company_seq", sa.Integer, nullable=False, server_default="0"), sa.Column("seq", sa.Integer, nullable=False, unique=True))
    op.create_index("uq_company_directory_seq", "company_industry_map", ["industry_seq", "company_seq"], unique=True)
    op.create_table("review_rounds", *common(),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("round", sa.Integer, nullable=False), sa.Column("stage", sa.String, nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_id", sa.Integer, sa.ForeignKey("review_snapshots.id"), nullable=False),
        sa.Column("execution_session_id", sa.Integer, sa.ForeignKey("sessions.id")),
        sa.Column("path_change", sa.JSON),
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
            raise RuntimeError("企业对照表中行业名和序号冲突，需先核实历史数据")
        seen.add(name)
        values.append({"name": name, "seq": seq, "created_at": now, "updated_at": now})
    if values:
        op.bulk_insert(table, values)

    op.execute("UPDATE industries SET last_company_seq=(SELECT MAX(company_seq) FROM company_industry_map WHERE industry_name=industries.name)")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER rounds_locked_{action} BEFORE {action} ON review_rounds WHEN OLD.decision IS NOT NULL
                       BEGIN SELECT RAISE(ABORT,'审核决定不可更改'); END""")
    for action, refs in (("INSERT", ["NEW"]), ("UPDATE", ["OLD", "NEW"]), ("DELETE", ["OLD"])):
        locked = " OR ".join(f"(SELECT decision FROM review_rounds WHERE id={ref}.review_round_id) IS NOT NULL" for ref in refs)
        op.execute(f"""CREATE TRIGGER pins_locked_{action} BEFORE {action} ON review_pins WHEN {locked}
                       BEGIN SELECT RAISE(ABORT,'已决定的审核意见不可更改'); END""")
    op.execute("""UPDATE batches SET payload=json_set(payload,'$.stage_since',
        (SELECT submitted_at FROM review_snapshots WHERE batch_id=batches.id AND round=batches.round))
        WHERE status='待初审' AND EXISTS (SELECT 1 FROM review_snapshots WHERE batch_id=batches.id AND round=batches.round)""")


def downgrade() -> None:
    raise RuntimeError("降级将丢失审核历史，请从升级前备份恢复")
