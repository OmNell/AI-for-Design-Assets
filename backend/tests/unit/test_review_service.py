"""核对服务的纯规则：待处理计数与自由文本标签修改。"""
import pytest

from app.db.models import BatchProduct
from app.errors import AppError
from app.services.review import ReviewService

KEYS = {"category", "material"}


def tag(value: str, source: str = "ai", flag: str | None = None) -> dict[str, object]:
    return {"value": value, "terms": [value] if value else [], "source": source, "ai_value": value, "flag": flag}


def product(kind: str = "item", **values: object) -> BatchProduct:
    defaults: dict[str, object] = {"ai_status": "done", "tags": {}, "flags": [], "relation_items": [], "relation_confirmed": False}
    return BatchProduct(kind=kind, display_name="图", **{**defaults, **values})


def pending(*products: BatchProduct) -> dict[str, object]:
    return ReviewService._pending(ReviewService.__new__(ReviewService), list(products), KEYS).model_dump()


def test_pending_counts_each_blocking_condition_separately() -> None:
    assert pending(product(tags={"category": tag("保温杯")}))["can_submit"] is True
    assert pending(product(tags={"category": tag("无法判断", flag="uncertain")})) == {
        "yellow": 1, "relations_unconfirmed": 0, "failed": 0, "processing": 0, "can_submit": False}
    assert pending(product(flags=["multi_object"]))["yellow"] == 1
    assert pending(product("set", flags=["empty_relation"])) == {
        "yellow": 0, "relations_unconfirmed": 1, "failed": 0, "processing": 0, "can_submit": False}
    assert pending(product("set", relation_confirmed=True))["can_submit"] is True
    assert pending(product(ai_status="failed"))["failed"] == 1
    assert pending(product(ai_status="pending"))["processing"] == 1


def test_failed_image_counts_as_handled_only_with_manual_tag_value() -> None:
    assert pending(product(ai_status="failed", tags={"category": tag("保温杯", "manual")}))["can_submit"] is True
    assert pending(product(ai_status="failed", tags={"category": tag("", "manual")}))["failed"] == 1
    assert pending(product(ai_status="failed", tags={"category": tag("保温杯", "ai")}))["failed"] == 1


def test_flags_on_disabled_fields_do_not_block() -> None:
    assert pending(product(tags={"retired": tag("无法判断", flag="uncertain")}))["can_submit"] is True
    assert pending(product(ai_status="failed", tags={"retired": tag("x", "manual")}))["failed"] == 1


def test_set_tags_trims_splits_keeps_ai_value_and_skips_unchanged() -> None:
    item = product(tags={"category": tag("无法判断", flag="uncertain"), "material": tag("陶瓷")})
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    ReviewService._set_tags(item, {"category": " 保温杯；雨伞 ", "material": "陶瓷 "}, KEYS, before, after)
    assert item.tags["category"] == {"value": "保温杯；雨伞", "terms": ["保温杯", "雨伞"], "source": "manual", "ai_value": "无法判断", "flag": None}
    assert item.tags["material"]["source"] == "ai"
    assert before == {"category": {"value": "无法判断", "source": "ai", "flag": "uncertain"}}
    assert after == {"category": {"value": "保温杯；雨伞", "source": "manual", "flag": None}}


def test_set_tags_rejects_only_overlong_or_unknown_fields() -> None:
    item = product()
    ReviewService._set_tags(item, {"category": "值" * 500}, KEYS, {}, {})
    assert len(item.tags["category"]["value"]) == 500
    with pytest.raises(AppError) as long:
        ReviewService._set_tags(item, {"category": "值" * 501}, KEYS, {}, {})
    assert long.value.code == "value_too_long" and long.value.status_code == 422
    with pytest.raises(AppError) as unknown:
        ReviewService._set_tags(item, {"other": "x"}, KEYS, {}, {})
    assert unknown.value.code == "unknown_field"
