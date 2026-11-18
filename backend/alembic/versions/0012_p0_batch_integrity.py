"""建立批次互斥、历史编号计数器和正式指纹算法标记。"""
import re

from alembic import op
import sqlalchemy as sa

revision = "0012_p0_batch_integrity"
down_revision = "0011_p0_device_sessions"
branch_labels = None
depends_on = None

LOCK_KEY = "CASE WHEN kind = 'ingest' THEN 'ingest:' || project_code ELSE 'project:' || project_id END"
LOCK_STATE = "CASE WHEN status IN ('已入库', '已撤回', '已终止') THEN 0 ELSE 1 END"


def upgrade() -> None:
    conn = op.get_bind()
    conflicts = conn.execute(sa.text(f"""
        SELECT group_concat(id) FROM batches WHERE ({LOCK_STATE}) = 1
        GROUP BY ({LOCK_KEY}) HAVING count(*) > 1 OR ({LOCK_KEY}) IS NULL
    """)).scalars().all()
    if conflicts:
        raise RuntimeError(f"存在无法同时持锁的在途批次，需先人工核对批次 ID：{'; '.join(conflicts)}")

    op.add_column("batches", sa.Column("terminated_at", sa.DateTime(timezone=True)))
    op.add_column("batches", sa.Column("termination_reason", sa.Text()))
    op.execute(f"ALTER TABLE batches ADD COLUMN op_lock_key TEXT GENERATED ALWAYS AS ({LOCK_KEY}) VIRTUAL")
    op.execute(f"""
        ALTER TABLE batches ADD COLUMN lock_active BOOLEAN GENERATED ALWAYS AS ({LOCK_STATE}) VIRTUAL
        CHECK (lock_active = 0 OR coalesce(length(op_lock_key), 0) > 0)
    """)
    op.create_index("uq_batches_active_lock", "batches", ["op_lock_key"], unique=True, sqlite_where=sa.text("lock_active = 1"))

    seq = op.create_table("h_code_sequences",
                          sa.Column("year", sa.Integer(), primary_key=True),
                          sa.Column("last_sequence", sa.Integer(), nullable=False))
    years: dict[int, int] = {}
    for code in conn.execute(sa.text("SELECT project_code FROM projects UNION SELECT project_code FROM batches")).scalars():
        match = re.fullmatch(r"H([0-9]{4})-([0-9]{4})", code)
        if match:
            year, number = int(match[1]), int(match[2])
            years[year] = max(years.get(year, 0), number)
    if years:
        op.bulk_insert(seq, [{"year": year, "last_sequence": number} for year, number in sorted(years.items())])

    op.add_column("batch_files", sa.Column("fingerprint_kind", sa.String()))
    op.execute("""
        UPDATE batch_files
        SET fingerprint_kind = CASE
            WHEN json_extract((SELECT payload FROM batches WHERE id = batch_files.batch_id),
                              '$.fingerprint_kinds."' || id || '"') = 'quick' THEN 'quick'
            WHEN json_extract((SELECT payload FROM batches WHERE id = batch_files.batch_id),
                              '$.fingerprint_kinds."' || id || '"') = 'full'
                AND length(fingerprint) = 64 AND lower(fingerprint) NOT GLOB '*[^0-9a-f]*' THEN 'full'
            ELSE 'legacy_unknown' END
        WHERE fingerprint IS NOT NULL
    """)


def downgrade() -> None:
    raise RuntimeError("请从升级前的备份恢复，以保留编号、终止与指纹历史")
