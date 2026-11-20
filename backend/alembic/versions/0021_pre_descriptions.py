"""为待审商品追加人工预描述和对应的视觉识别版本。"""
import json
from pathlib import PurePosixPath
import re

from alembic import op
import sqlalchemy as sa

revision = "0021_pre_descriptions"
down_revision = "0020_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("batch_products", sa.Column("pre_description", sa.Text(), nullable=True))
    # SQLite 支持新增可空的内联外键；避免复制历史表和索引。
    op.execute("ALTER TABLE batch_products ADD COLUMN pre_description_by INTEGER REFERENCES users(id)")
    op.add_column("batch_products", sa.Column("pre_description_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("batch_products", sa.Column("pre_description_fingerprint", sa.String(64), nullable=True))
    op.add_column("batch_products", sa.Column("pre_description_version", sa.Integer(),
                  server_default="0", nullable=False))
    op.add_column("batch_products", sa.Column("ai_description_version", sa.Integer(), nullable=True))
    op.add_column("batch_products", sa.Column("ai_raw_result", sa.Text(), nullable=True))

    connection = op.get_bind()
    missing = connection.execute(sa.text("""
        SELECT file.batch_id, file.id, file.rel_path
        FROM batch_files AS file JOIN batches AS batch ON batch.id = file.batch_id
        WHERE batch.kind IN ('ingest', 'supplement')
          AND batch.status IN ('已暂存', 'AI 处理中', 'AI 处理失败', '待核对', '已驳回')
          AND file.upload_status = 'done' AND file.removed = 0
          AND file.size > 0 AND file.bytes_received = file.size
          AND (file.conflict IS NULL OR file.conflict != 'duplicate')
          AND NOT EXISTS (SELECT 1 FROM batch_products AS product
                          WHERE product.batch_id = file.batch_id AND product.batch_file_id = file.id)
    """)).mappings()
    products = []
    for row in missing:
        relative = row["rel_path"]
        if re.fullmatch(r"(?:套装|单品)/[^/]+\.(?:jpg|jpeg|png)", relative, re.IGNORECASE):
            path = PurePosixPath(relative)
            products.append({"batch_id": row["batch_id"], "file_id": row["id"], "name": path.stem,
                             "kind": "set" if path.parts[0] == "套装" else "item"})
    if products:
        connection.execute(sa.text("""
            INSERT INTO batch_products (
                created_at, updated_at, batch_id, batch_file_id, kind, display_name, ai_status, tags,
                free_text, free_text_source, ai_free_text, tags_confirmation, flags, relation_items,
                relation_confirmed, relation_confirm_kind)
            VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :batch_id, :file_id, :kind, :name, 'awaiting_description',
                '{}', '', 'ai', '', '{}', '[]', '[]', 0, 'pending')
        """), products)

    # 升级前旧任务没有描述版本。先释放编辑权，已验证的结果和历史记录继续保留。
    interrupted = connection.execute(sa.text("""
        SELECT id, status FROM batches WHERE kind IN ('ingest', 'supplement')
          AND (status = 'AI 处理中' OR (status IN ('待核对', '已驳回')
                    AND json_extract(payload, '$.ai_single') NOT IN ('{}', 'null')))
    """)).mappings().all()
    for batch in interrupted:
        params = {"id": batch["id"]}
        connection.execute(sa.text("""
            UPDATE batch_products AS product
            SET ai_status = CASE WHEN EXISTS (
                  SELECT 1 FROM batch_files AS file
                  WHERE file.id = product.batch_file_id AND file.batch_id = product.batch_id
                    AND file.removed = 0 AND file.upload_status = 'done' AND file.fingerprint_kind = 'full'
                    AND file.fingerprint = product.ai_fingerprint AND product.ai_success_at IS NOT NULL
                    AND product.ai_model IS NOT NULL
                ) THEN 'done' ELSE 'awaiting_description' END,
                ai_error = NULL
            WHERE product.batch_id = :id AND product.ai_status = 'pending'
        """), params)
        status = batch["status"]
        if status == "AI 处理中":
            proven = connection.execute(sa.text("""
                SELECT 1 FROM batch_products AS product JOIN batch_files AS file ON file.id = product.batch_file_id
                WHERE product.batch_id = :id AND product.ai_status = 'done' AND file.removed = 0
                  AND file.upload_status = 'done' AND file.fingerprint_kind = 'full'
                  AND file.fingerprint = product.ai_fingerprint AND product.ai_success_at IS NOT NULL
                  AND product.ai_model IS NOT NULL LIMIT 1
            """), params).scalar()
            status = "待核对" if proven else "AI 处理失败"
        connection.execute(sa.text("""
            UPDATE batches SET status = :status, updated_at = CURRENT_TIMESTAMP,
                payload = json_set(payload, '$.ai_single', json('{}'), '$.ai_jobs', json('{}'))
            WHERE id = :id
        """), {**params, "status": status})
        connection.execute(sa.text("""
            UPDATE jobs SET status = 'failed', error = '升级后须补填主图描述再重试', finished_at = CURRENT_TIMESTAMP
            WHERE kind IN ('ai_tag_batch', 'ai_tag_product') AND status IN ('queued', 'running')
              AND json_extract(payload, '$.batch_id') = :id
        """), params)
        connection.execute(sa.text("""
            INSERT INTO oplog (created_at, updated_at, ts, role, action, object_type, object_id, before, after, result, client_host)
            VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'system', '结束升级前未完成的 AI 任务',
                    'batch', CAST(:id AS TEXT), :before, :after, '成功', '数据库迁移')
        """), {**params, "before": json.dumps({"status": batch["status"]}, ensure_ascii=False),
                "after": json.dumps({"status": status, "reason": "未完成的主图需补填描述后重新识别"}, ensure_ascii=False)})



def downgrade() -> None:
    for name in ("ai_raw_result", "ai_description_version", "pre_description_version",
                 "pre_description_fingerprint", "pre_description_at", "pre_description_by", "pre_description"):
        op.drop_column("batch_products", name)
