"""检索的请求与响应。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Condition(BaseModel):
    """系统理解出的一个条件；terms 第一项是关键词，其余是同义说法。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["field", "exclude", "other", "company"]
    field: str | None = Field(default=None, max_length=64)
    label: str = Field(default="", max_length=64)
    terms: list[str] = Field(min_length=1, max_length=12)


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    won: bool | None = None
    industry: int | None = None
    company: int | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    # 页面删除某个条件后把剩下的条件原样带回，此时不再调用模型理解。
    conditions: list[Condition] | None = Field(default=None, max_length=40)
    page: int = Field(default=1, ge=1, le=10)


class RelatedProduct(BaseModel):
    id: int
    product_code: str
    display_name: str


class ResultProject(BaseModel):
    id: int
    project_code: str
    project_name: str
    company_name: str
    won: bool
    link_id: str | None


class ResultTag(BaseModel):
    key: str
    label: str
    value: str


class SearchResult(BaseModel):
    id: int
    product_code: str
    display_name: str
    kind: str
    tags: list[ResultTag]
    free_text: str
    related: list[RelatedProduct]
    project: ResultProject
    link_id: str | None
    reason: str | None
    matched: list[str]
    by_keywords: bool
    by_vector: bool
    similarity: float | None


class ProjectHit(BaseModel):
    id: int
    project_code: str
    project_name: str
    company_name: str
    won: bool
    link_id: str | None


class SearchResponse(BaseModel):
    log_id: int
    query: str
    kind: Literal["code", "natural"]
    conditions: list[Condition]
    relaxed: bool
    degraded: bool
    notes: list[str]
    from_cache: bool
    model_calls: int
    elapsed_ms: int
    local_ms: int
    total: int
    page: int
    page_size: int
    results: list[SearchResult]
    project_hits: list[ProjectHit]


class ClickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    log_id: int = Field(gt=0)
    product_id: int = Field(gt=0)


class IndexStatus(BaseModel):
    backend: str
    model: str
    products: int
    vectors: int
    dim: int | None
    queued: int
