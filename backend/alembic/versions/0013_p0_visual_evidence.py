"""固定标签字段身份，并记录待审主图的视觉识别成功证据。"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0013_p0_visual_evidence"
down_revision = "0012_p0_batch_integrity"
branch_labels = None
depends_on = None

FIELDS = (
    ("category", "品类", "这是什么东西"), ("material", "材质", "主要材料"),
    ("color", "主色", "主要颜色"), ("style", "风格", "设计风格"),
    ("festival", "节日场景", "适用的具体节日；不针对节日时留空"),
    ("audience", "受众群体", "给谁用、送给谁"), ("usage_scene", "使用场景", "用途和使用的场合"),
    ("size", "尺寸", "大小的描述"), ("craft", "工艺", "制作或装饰工艺"),
    ("material_name", "物料名称", "物料的称呼"), ("structure", "结构", "组成部分和形态结构"),
)


def upgrade() -> None:
    conn = op.get_bind()
    rows = {row["key"]: dict(row) for row in conn.execute(
        sa.text("SELECT id, key, label, hint, sort_order, enabled, version FROM tag_fields")
    ).mappings()}
    canonical = {key for key, _, _ in FIELDS}
    version = max((row["version"] for row in rows.values()), default=1)
    used_order = set()
    next_order = max((row["sort_order"] for row in rows.values()), default=-1) + 1
    changes = []
    for key, label, default_hint in FIELDS:
        row = rows.get(key)
        order = row["sort_order"] if row else next_order
        if order in used_order:
            order = next_order
        used_order.add(order)
        next_order = max(next_order, order + 1)
        hint = row["hint"] if row else default_hint
        if key == "festival" and hint == "适用的具体节日；不针对节日就写「通用」":
            hint = default_hint
        after = {"label": label, "enabled": 1, "hint": hint, "sort_order": order}
        if row is None:
            conn.execute(sa.text("""INSERT INTO tag_fields
                (key, label, enabled, hint, sort_order, version, created_at, updated_at)
                VALUES (:key, :label, :enabled, :hint, :sort_order, :version, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"key": key, "version": version, **after})
        elif any(row[column] != value for column, value in after.items()):
            conn.execute(sa.text("""UPDATE tag_fields SET label=:label, enabled=:enabled, hint=:hint,
                sort_order=:sort_order, updated_at=CURRENT_TIMESTAMP WHERE id=:id"""), {"id": row["id"], **after})
        else:
            continue
        changes.append((key, row, after))
    for key, row in rows.items():
        if key not in canonical and row["enabled"]:
            conn.execute(sa.text("UPDATE tag_fields SET enabled = 0 WHERE id = :id"), {"id": row["id"]})
            changes.append((key, row, {"enabled": 0}))

    if changes:
        conn.execute(sa.text("UPDATE tag_fields SET version = :version"), {"version": version + 1})
        for key, before, after in changes:
            if key == "festival" and before is not None and all(before[field] == after[field]
                    for field in ("label", "enabled", "sort_order")):
                # 标准提示的文案差异可从历史迁移查阅，不为新库制造操作记录。
                continue
            conn.execute(sa.text("""INSERT INTO oplog
                (created_at, updated_at, ts, role, action, object_type, object_id, before, after, result, client_host)
                VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'system',
                    '适配固定标签字段', 'tag_field', :key, :before, :after, '成功', '数据库迁移')
            """), {"key": key, "before": json.dumps(before, ensure_ascii=False), "after": json.dumps(after, ensure_ascii=False)})
    op.execute("""CREATE TRIGGER tag_fields_no_insert BEFORE INSERT ON tag_fields
                  BEGIN SELECT RAISE(ABORT, '标签字段清单固定'); END""")
    op.execute("""CREATE TRIGGER tag_fields_no_delete BEFORE DELETE ON tag_fields
                  BEGIN SELECT RAISE(ABORT, '标签字段清单固定'); END""")
    op.execute("""CREATE TRIGGER tag_fields_identity BEFORE UPDATE OF key, label, enabled ON tag_fields
                  WHEN NEW.key != OLD.key OR NEW.label != OLD.label OR NEW.enabled != OLD.enabled
                  BEGIN SELECT RAISE(ABORT, '标签字段标识和名称固定'); END""")

    op.add_column("batch_files", sa.Column("thumb_fingerprint", sa.String()))
    op.add_column("batch_products", sa.Column("ai_success_at", sa.DateTime(timezone=True)))
    op.add_column("batch_products", sa.Column("ai_model", sa.String()))
    op.add_column("batch_products", sa.Column("ai_fingerprint", sa.String()))


def downgrade() -> None:
    raise RuntimeError("请从升级前的备份恢复，以保留标签历史和视觉识别证据")
