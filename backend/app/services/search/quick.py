"""快速匹配只在 SQLite 对正式字段计分。"""
from __future__ import annotations

import json
import re
import unicodedata

from sqlalchemy import Integer, Select, case, cast, func, literal, select, true, union_all
from sqlalchemy.sql.selectable import CTE, Subquery

from app.db.models import Product, ProductTag, Project
from app.schemas.search import SearchConditions, SearchRequest
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.catalog import CatalogService

PRIMARY_FIELDS = ("category", "material_name", "material")
FACETS = {"name": "名称", "code": "编号", "kind": "商品类型", "description": "自由描述",
          "company": "企业", "industry": "行业", "won": "中标状态", **FIXED_FIELDS}


def parse(body: SearchRequest) -> tuple[str, SearchConditions]:
    query = " ".join(unicodedata.normalize("NFKC", body.query).lower().split())[:250]
    # 保留编号中的连字符，按空白和标点分隔整词。
    words = list(dict.fromkeys(re.findall(r"[^\W_]+(?:-[^\W_]+)*", query)))[:12]
    removed = set(body.remove)
    kept = [word for word in words if f"keyword:{word}" not in removed]
    return (query if kept == words else " ".join(kept)), SearchConditions(keywords=kept)


def _words(terms: list[str]) -> CTE:
    rows = [select(literal(term).label("term")) for term in terms]
    return (rows[0].union_all(*rows[1:]) if len(rows) > 1 else rows[0]).cte("search_words")


def _signal(priority: int, field: str):
    return literal(f"{priority:02d}:{field}")


def _totals(hits: Subquery) -> Subquery:
    # 同一关键词在多个字段命中也只计一次，用优先级最高的字段解释。
    strongest = (select(hits.c.id, hits.c.term, func.min(hits.c.signal).label("signal"))
                 .where(hits.c.signal.is_not(None)).group_by(hits.c.id, hits.c.term).subquery())
    return select(strongest.c.id, func.count().label("coverage"), func.min(strongest.c.signal).label("best"),
                  func.sum(cast(func.substr(strongest.c.signal, 1, 2), Integer)).label("total"),
                  func.json_group_array(func.json_array(strongest.c.term, strongest.c.signal)).label("evidence")
                  ).group_by(strongest.c.id).subquery()


def ranked_products(scope: Select[tuple[Product]], query: str, terms: list[str]) -> Select[tuple[Product, int, str]]:
    words = _words(terms)
    token = words.c.term
    source = scope.with_only_columns(Product.id.label("id"), Product.display_name.label("name"),
                                     Product.kind, Product.free_text).order_by(None).cte("searchable_products")
    name = func.lower(source.c.name)
    kind = case((source.c.kind == "item", "单品"), (source.c.kind == "set", "套装"))
    primary = case(
        (name == token, _signal(2, "name")),
        (func.instr(name, token) == 1, _signal(3, "name")),
        (func.instr(name, token) > 0, _signal(4, "name")),
        ((kind == token) | (source.c.kind == token), _signal(6, "kind")),
        (func.instr(func.lower(source.c.free_text), token) > 0, _signal(7, "description")),
        else_=None,
    )
    direct = select(source.c.id, token, primary.label("signal")).select_from(source.join(words, true()))
    tag = ProductTag
    pieces = func.json_each(tag.terms).table_valued("value")
    term_hit = select(1).select_from(pieces).where(func.lower(pieces.c.value) == token).correlate_except(pieces).exists()
    tag_rank = case((tag.field_key.in_(PRIMARY_FIELDS), "05:"), else_="06:") + tag.field_key
    tags = (select(source.c.id, token, tag_rank.label("signal"))
            .select_from(source.join(tag, tag.product_id == source.c.id).join(words, true()))
            .where(tag.confirmation_status == "confirmed_value", tag.field_key.in_(FIXED_FIELDS),
                   func.trim(tag.value).not_in(("", "无法确定", "无法判断")),
                   (func.instr(func.lower(tag.value), token) > 0) | term_hit))
    ranked = _totals(union_all(direct, tags).subquery())
    full_name = case((func.lower(Product.display_name) == query, 0), else_=1)
    return (select(Product, ranked.c.coverage, ranked.c.evidence).join(ranked, ranked.c.id == Product.id)
            .order_by(ranked.c.coverage.desc(), full_name, func.substr(ranked.c.best, 1, 2), ranked.c.total, Product.id))


def ranked_projects(scope: Select[tuple[Project]], query: str, terms: list[str]) -> Select[tuple[Project, int, str]]:
    words = _words(terms)
    token = words.c.term
    name = func.lower(Project.project_name)
    won = case((Project.won, "已中标"), else_="未中标")
    signal = case(
        (name == token, _signal(2, "name")),
        (func.instr(name, token) > 0, _signal(4, "name")),
        (func.instr(func.lower(Project.project_code), token) > 0, _signal(4, "code")),
        (CatalogService.company_matches(token), _signal(5, "company")),
        (func.instr(func.lower(CatalogService.industry_name()), token) > 0, _signal(6, "industry")),
        (token == won, _signal(6, "won")),
        else_=None,
    )
    counts = _totals(scope.with_only_columns(Project.id, token, signal.label("signal")).order_by(None)
                     .join(words, true()).subquery())
    full_name = case((name == query, 0), else_=1)
    return (select(Project, counts.c.coverage, counts.c.evidence).join(counts, counts.c.id == Project.id)
            .order_by(counts.c.coverage.desc(), full_name, func.substr(counts.c.best, 1, 2), counts.c.total, Project.id))


def explain(coverage: int, data: str, words: list[str]) -> tuple[str, list[str]]:
    evidence = {word: signal.split(":", 1)[1] for word, signal in json.loads(data)}
    matched = [(word, evidence[word]) for word in words if word in evidence]
    reasons = "；".join(f"{FACETS[field]}匹配：{word}" for word, field in matched)
    if coverage < len(words):
        reasons = f"部分关键词匹配（{coverage}/{len(words)}） · {reasons}"
    return reasons, list(dict.fromkeys(field for _, field in matched if field in FIXED_FIELDS))
