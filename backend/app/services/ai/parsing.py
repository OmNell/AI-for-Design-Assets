"""自由文本标签与关系序号解析，不进行词表或取值校验。"""
from __future__ import annotations

import json
import re
from typing import TypedDict


class FieldDefinition(TypedDict):
    key: str
    label: str
    hint: str


class TagValue(TypedDict):
    value: str
    terms: list[str]
    source: str
    ai_value: str
    flag: str | None


class RelationItem(TypedDict):
    batch_product_id: int
    source: str


class ParsedResult(TypedDict):
    tags: dict[str, TagValue]
    free_text: str
    flags: list[str]
    relation_items: list[RelationItem]


class AiResponseError(ValueError):
    """模型输出格式不完整，调用方可以按任务重试策略重试。"""


def split_terms(value: str) -> list[str]:
    return list(dict.fromkeys(term for term in re.split(r"[、，,；;/\s]+", value.strip()) if term))


def parse_result(raw: str, fields: list[FieldDefinition], kind: str = "item",
                 valid_relations: dict[int, int] | None = None) -> ParsedResult:
    content = raw.strip().lstrip("\ufeff")
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    try:
        document: object = json.loads(content)
    except (ValueError, TypeError):
        raise AiResponseError("模型未返回有效 JSON") from None
    if not isinstance(document, dict):
        raise AiResponseError("模型 JSON 必须是对象")
    source = document.get("tags", document)
    if not isinstance(source, dict):
        raise AiResponseError("模型标签必须是对象")
    description = document.get("free_text", "")
    if not any(isinstance(source.get(field["key"]), str) for field in fields) and not (isinstance(description, str) and description.strip()):
        raise AiResponseError("模型未返回识别内容")
    tags: dict[str, TagValue] = {}
    for field in fields:
        raw_value = source.get(field["key"])
        ai_value = raw_value if isinstance(raw_value, str) else ""
        uncertain = not isinstance(raw_value, str) or ai_value.strip() in {"无法判断", "无法确定"}
        value = "" if uncertain else ai_value
        if field["key"] == "festival" and value.strip() == "通用":
            value = ""
        tags[field["key"]] = {"value": value, "terms": split_terms(value), "source": "ai",
                              "ai_value": ai_value, "flag": "uncertain" if uncertain else None}
    flags: list[str] = ["description_conflict"] if document.get("description_conflict") is True else []
    if kind == "item" and document.get("multi_object") is True:
        flags.append("multi_object")
    relations: list[RelationItem] = []
    if kind == "set":
        candidates = document.get("relation_items", [])
        seen: set[int] = set()
        allowed = valid_relations or {}
        if isinstance(candidates, list):
            for value in candidates:
                # JSON 的 true 不是候选序号 1；只容忍整数字符串，不猜测其他表示。
                index = int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) else value
                if type(index) is int and index in allowed and allowed[index] not in seen:
                    product_id = allowed[index]
                    seen.add(product_id)
                    relations.append({"batch_product_id": product_id, "source": "ai"})
        if not relations:
            flags.append("empty_relation")
    return {"tags": tags, "free_text": description if isinstance(description, str) else "",
            "flags": flags, "relation_items": relations}
