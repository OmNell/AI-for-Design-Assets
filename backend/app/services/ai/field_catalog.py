"""正式标签字段的固定身份。显示顺序与填写说明单独保存在数据库。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TagField

FIXED_FIELDS = {
    "category": "品类", "material": "材质", "color": "主色", "style": "风格",
    "festival": "节日场景", "audience": "受众群体", "usage_scene": "使用场景",
    "size": "尺寸", "craft": "工艺", "material_name": "物料名称", "structure": "结构",
}


def fixed_fields(session: Session) -> list[TagField]:
    return list(session.scalars(select(TagField).where(TagField.key.in_(FIXED_FIELDS)).order_by(TagField.sort_order, TagField.id)))
