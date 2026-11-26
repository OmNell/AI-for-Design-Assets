"""生成固定字段的识别指令；单品候选仅保留当前识别所需的数据。"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template

from .parsing import FieldDefinition

PROMPT_DIRECTORY = Path(__file__).resolve().parents[2] / "prompts"


def build_prompt(fields: list[FieldDefinition], items: list[dict[str, object]] | None = None) -> str:
    field_text = "\n".join(f'- {field["key"]}（{field["label"]}）：{field["hint"]}' for field in fields)
    candidates: list[dict[str, object]] = []
    enabled_keys = {field["key"] for field in fields}
    for item in items or []:
        if type(item.get("index")) is not int or not isinstance(item.get("name"), str):
            continue
        source = item.get("tags", {})
        tags: dict[str, str] = {}
        if isinstance(source, dict):
            for key, value in source.items():
                if isinstance(key, str) and key in enabled_keys:
                    if isinstance(value, str):
                        tags[key] = value
                    elif isinstance(value, dict) and isinstance(value.get("value"), str):
                        tags[key] = value["value"]
        candidates.append({"index": item["index"], "name": item["name"], "tags": tags})
    name = "set_tagging.txt" if items is not None else "item_tagging.txt"
    template = Template((PROMPT_DIRECTORY / name).read_text(encoding="utf-8"))
    return template.substitute(fields=field_text, items=json.dumps(candidates, ensure_ascii=False))
