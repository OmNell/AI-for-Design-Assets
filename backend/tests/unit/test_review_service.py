"""固定字段草稿和快照摘要的纯规则。"""
import pytest

from app.db.models import BatchProduct
from app.errors import AppError
from app.services.review import ReviewService


def test_snapshot_digest_is_canonical_and_includes_business_values() -> None:
    first = {"批次": 1, "标签": {"材质": "木", "主色": "红"}}
    second = {"标签": {"主色": "红", "材质": "木"}, "批次": 1}
    assert ReviewService._digest(first) == ReviewService._digest(second)
    second["标签"]["主色"] = "蓝"
    assert ReviewService._digest(first) != ReviewService._digest(second)


def test_draft_changes_keep_original_text_and_reset_confirmation() -> None:
    old = {"value": "瓷", "terms": ["瓷"], "ai_value": "瓷", "source": "ai_confirmed", "flag": None}
    item = BatchProduct(tags={"material":old}, tags_confirmation={"material":{"status":"confirmed_value","previous_value":"铁"}})
    before, after = {}, {}
    ReviewService._set_tags(item, {"material":" 纸、木 纸 "}, {"material"}, before, after)
    assert item.tags["material"]["terms"] == ["纸","木"] and item.tags["material"]["ai_value"] == "瓷"
    assert item.tags_confirmation["material"] == {"status":"pending","previous_value":"铁"}
    assert before["material"]["tag"] == old and after["material"]["tag"]["source"] == "manual"
    with pytest.raises(AppError, match="500"):
        ReviewService._set_tags(item, {"material":"木" * 501}, {"material"}, {}, {})
