"""检索和向量索引接口。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.schemas.catalog import ProjectRow
from app.services.ai.field_catalog import FIXED_FIELDS

SearchMode = Literal["quick", "semantic"]

Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class Conditions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fields: dict[str, list[Term]] = Field(default_factory=dict, max_length=11)
    keywords: list[Term] = Field(default_factory=list, max_length=12)
    exclude: list[Term] = Field(default_factory=list, max_length=8)

    @field_validator("fields")
    @classmethod
    def fixed_keys(cls, fields: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(key not in FIXED_FIELDS or not 1 <= len(terms) <= 6 for key, terms in fields.items()):
            raise ValueError("字段或同义表达数量不符合要求")
        return fields


class SearchConditions(Conditions):
    # 快速匹配保留原始整词，查询模型的输出仍受原有 Term 上限约束。
    keywords: list[Annotated[str, StringConstraints(min_length=1, max_length=250)]] = Field(default_factory=list, max_length=12)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)]
    mode: SearchMode = "semantic"
    won: bool | None = None
    industry: str = Field(default="", max_length=120)
    company: str = Field(default="", max_length=120)
    remove: list[str] = Field(default_factory=list, max_length=24)
    limit: int = Field(default=10, ge=1, le=10)
    include_candidates: bool = False


class ClickRequest(BaseModel):
    search_id: int = Field(gt=0)
    kind: Literal["project", "product"]
    id: int = Field(gt=0)


class CallUsage(BaseModel):
    model: str
    kind: str
    input_tokens: int
    output_tokens: int
    success: bool


class SearchHit(BaseModel):
    id: int
    project_id: int
    product_code: str
    name: str
    kind: str
    project_name: str
    project_code: str
    company_name: str
    won: bool
    project_url: str | None
    url: str | None
    tags: dict[str, str]
    related: list[str]
    matches: list[str]
    keyword_score: float
    similarity: float | None
    reason: str


class SearchProjectHit(ProjectRow):
    reason: str | None = None


class SearchResult(BaseModel):
    search_id: int
    mode: SearchMode
    products: list[SearchHit]
    projects: list[SearchProjectHit]
    conditions: SearchConditions
    warnings: list[str]
    degraded: bool
    exact: bool
    permission_filtered: bool
    index_generation: int | None
    candidate_codes: list[str]
    project_candidate_codes: list[str] = Field(default_factory=list)
    keyword_candidates: int
    vector_candidates: int
    field_labels: dict[str, str]
    usage: list[CallUsage]
    elapsed_ms: int


class BuildIndex(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    revision: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""


class IndexInfo(BaseModel):
    generation: int
    name: str
    version: str
    revision: str
    status: str
    dimension: int | None
    total: int
    ready: int
    failures: int
    pending: int
    verified: bool
    running: bool


class IndexStatus(BaseModel):
    backend: str
    indexes: list[IndexInfo]
