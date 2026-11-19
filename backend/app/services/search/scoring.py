"""关键词、排除词及确定性排序融合。"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.schemas.search import Conditions


@dataclass
class Candidate:
    id: int
    name: str
    description: str
    tags: dict[str, str]
    terms: dict[str, list[str]]


def _normalized(text: str) -> str:
    return text.casefold().strip()


def _has(terms: list[str], text: str) -> bool:
    text = _normalized(text)
    return any(_normalized(term) in text for term in terms if term.strip())


def lexical(candidate: Candidate, conditions: Conditions) -> tuple[float, list[str], float] | None:
    fields = {key: " ".join([value, *candidate.terms.get(key, [])]) for key, value in candidate.tags.items()}
    corpus = " ".join([candidate.name, candidate.description, *fields.values()])
    if _has(conditions.exclude, corpus):
        return None
    matches = [key for key, terms in conditions.fields.items() if _has(terms, fields.get(key, ""))]
    score = sum(4 if key in ("category", "material_name", "material", "usage_scene") else 3 for key in matches)
    general = [term for term in conditions.keywords if _has([term], corpus)]
    score += sum(3 if _has([term], candidate.name) else 1 for term in general)
    relaxed = sum(1 for key, terms in conditions.fields.items() if key not in matches and _has(terms, corpus))
    return float(score), matches, float(relaxed)


def fallback(query: str) -> Conditions:
    negated = re.findall(r"(?:不需要|不要|不含|排除掉|排除)\s*([^，,。；;\s]+)", query)
    positive = re.split(r"不要|不含|排除|不需要", query, maxsplit=1)[0]
    terms = [word for word in re.split(r"[\s，,。；;、：:/]+", positive) if word]
    # 保留整词。中文长句的相邻双字提供保守的关键词降级。
    expanded = [word[:50] for word in terms[:12]]
    for term in terms:
        if len(term) >= 5 and re.fullmatch(r"[\u3400-\u9fff]+", term):
            expanded.extend(term[index:index + 2] for index in range(0, len(term) - 1))
    return Conditions(keywords=list(dict.fromkeys(expanded))[:12], exclude=[term[:50] for term in negated[:8]])


def fuse(lexical_ids: list[int], vector_ids: list[int], limit: int = 60) -> list[int]:
    # 倒数排名融合消除了字段分与余弦值的尺度差异。
    points: dict[int, float] = {}
    first_rank = {pid: i for i, pid in enumerate(lexical_ids)}
    for lane in (lexical_ids, vector_ids):
        for position, pid in enumerate(lane, 1):
            points[pid] = points.get(pid, 0.0) + 1 / (60 + position)
    return sorted(points, key=lambda pid: (-points[pid], first_rank.get(pid, 1000), pid))[:limit]
