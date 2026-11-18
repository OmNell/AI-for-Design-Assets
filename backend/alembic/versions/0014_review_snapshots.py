"""增加核对草稿和不可覆盖的每轮提交版本。"""
from alembic import op
import sqlalchemy as sa

revision = "0014_review_snapshots"
down_revision = "0013_p0_visual_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, kind, default in (
        ("free_text_source", sa.String(), "ai"), ("ai_free_text", sa.Text(), ""),
        ("tags_confirmation", sa.JSON(), "{}"), ("relation_confirm_kind", sa.String(), "pending"),
    ):
        op.add_column("batch_products", sa.Column(name, kind, nullable=False, server_default=default))
    op.execute("UPDATE batch_products SET ai_free_text = free_text")
    op.add_column("batches", sa.Column("review_snapshot_hash", sa.String(64)))
    op.create_table("review_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("submitted_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.UniqueConstraint("batch_id", "round"))
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER review_snapshot_no_{action.lower()} BEFORE {action} ON review_snapshots
            BEGIN SELECT RAISE(ABORT, '已提交的审核版本不可更改'); END""")


def downgrade() -> None:
    raise RuntimeError("降级会丢失人工确认与审核快照，请从升级前的完整备份恢复")
