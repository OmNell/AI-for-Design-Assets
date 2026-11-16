"""检索引擎：编号直达 → 查询理解与同义扩展 → 字段关键词匹配与向量两路海选 → 模型排序 → 组装。

标签是自由文本，没有标准值；「说法不一样」由同义扩展、向量相似度和模型排序共同解决。
发给外部模型的只有用户的话（去掉企业名与编号）和候选的文字描述，不含路径、编号、企业名。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import re
import time

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (CompanyIndustryMap, Job, Link, Product, ProductEmbedding, ProductRelation, ProductTag, Project, QueryCache,
                           SearchLog, TagField, User)
from app.errors import AppError
from app.schemas.search import (Condition, IndexStatus, ProjectHit, RelatedProduct, ResultProject, ResultTag, SearchFilters,
                                SearchRequest, SearchResponse, SearchResult)
from app.services.ai.client import AiCallError, AiClient
from app.services.ai.orchestrator import AiService
from app.services.auth.service import AuthService
from app.services.jobs import JobContext, JobFailure, JobQueue
from app.services.oplog import OplogService
from .vectors import VectorStore

PAGE_SIZE = 20
TOP_N = 10
CODE_LIKE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{1,39}")


def normalize(value: str) -> str:
    return "".join(value.split()).casefold()


def product_text(name: str, tags: list[tuple[str, str]], free_text: str) -> str:
    """向量与排序共用的固定拼接：名称 + 各字段标签 + 自由描述。"""
    lines = [f"名称：{name}", *(f"{label}：{value}" for label, value in tags if value.strip())]
    if free_text.strip():
        lines.append(f"描述：{free_text.strip()}")
    return "\n".join(lines)


def parse_json(raw: str) -> object:
    content = raw.strip().lstrip("﻿")
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    return json.loads(content)


def parse_conditions(raw: str, labels: dict[str, str]) -> list[Condition]:
    """把模型输出整理成条件列表；字段不认识、类型不对的内容一律丢弃。"""
    document = parse_json(raw)
    if not isinstance(document, dict):
        raise ValueError("查询理解结果必须是对象")
    result: list[Condition] = []

    def terms(values: list[object]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:40] for value in values if isinstance(value, str) and value.strip()))[:6]

    fields = document.get("fields", {})
    for key, groups in (fields.items() if isinstance(fields, dict) else []):
        if key not in labels or not isinstance(groups, list):
            continue
        for group in groups[:6]:
            if isinstance(group, dict):
                synonyms = group.get("synonyms", [])
                words = terms([group.get("keyword"), *(synonyms if isinstance(synonyms, list) else [])])
            else:
                words = terms([group])
            if words:
                result.append(Condition(type="field", field=key, label=labels[key], terms=words))
    for kind, label in (("exclude", "排除"), ("other", "关键词")):
        values = document.get(kind, [])
        for word in terms(values if isinstance(values, list) else [])[:8]:
            result.append(Condition(type=kind, label=label, terms=[word]))
    return result


def parse_ranking(raw: str, size: int, limit: int = TOP_N) -> list[tuple[int, str]]:
    """只接受 1..size 范围内、未重复的整数序号，防止模型编造候选。"""
    document = parse_json(raw)
    rows = document.get("ranking", []) if isinstance(document, dict) else []
    result: list[tuple[int, str]] = []
    seen: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        index = row.get("index") if isinstance(row, dict) else row
        if type(index) is not int or not 1 <= index <= size or index in seen:
            continue
        seen.add(index)
        reason = row.get("reason") if isinstance(row, dict) else ""
        result.append((index, reason.strip()[:200] if isinstance(reason, str) else ""))
        if len(result) == limit:
            break
    return result


@dataclass
class Candidate:
    product_id: int
    name: str
    free_text: str
    project_id: int
    tags: dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    matched: list[str] = field(default_factory=list)
    similarity: float | None = None
    reason: str | None = None

    def haystack(self) -> str:
        # 候选一侧只做大小写折叠；逐个去空白在几千个商品上是本地耗时的主要来源。
        return " ".join([self.name, self.free_text, *self.tags.values()]).casefold()


class SearchService:
    def __init__(self, auth: AuthService, queue: JobQueue, ai: AiService, store: VectorStore) -> None:
        self.auth, self.queue, self.ai, self.store = auth, queue, ai, store
        self.config = auth.config.search
        # 测试可替换成带模拟传输层的真实客户端，用来检查实际外发的内容。
        self.client_factory: Callable[[], AiClient] = ai.client

    # ---- 敏感信息 ----
    @staticmethod
    def _secrets(session: Session) -> list[str]:
        """不得出现在外发文字里的企业名、别名和立项编号（长的先替换）。"""
        values: set[str] = set()
        for row in session.scalars(select(CompanyIndustryMap)):
            values.update([row.company_name, *row.aliases])
        for code, company in session.execute(select(Project.project_code, Project.company_name)):
            values.update([code, company])
        return sorted((value for value in values if value and len(value) >= 2), key=len, reverse=True)

    @staticmethod
    def _scrub(value: str, secrets: list[str]) -> str:
        for secret in secrets:
            value = re.sub(re.escape(secret), "", value, flags=re.IGNORECASE)
        return value

    # ---- 向量索引 ----
    @staticmethod
    def _labels(session: Session, enabled_only: bool = False) -> dict[str, str]:
        query = select(TagField).order_by(TagField.sort_order, TagField.id)
        if enabled_only:
            query = query.where(TagField.enabled.is_(True))
        return {row.key: row.label for row in session.scalars(query)}

    def _document(self, session: Session, product: Product, secrets: list[str]) -> str:
        labels = self._labels(session)
        tags = {row.field_key: row.value for row in session.scalars(select(ProductTag).where(ProductTag.product_id == product.id))}
        return self._scrub(product_text(product.display_name, [(labels[key], tags[key]) for key in labels if key in tags], product.free_text), secrets)

    def enqueue(self, session: Session, product_id: int) -> None:
        """入库、修改标签或描述后调用；文本没变时任务会自己跳过。"""
        self.queue.enqueue("embed", {"product_id": product_id}, session=session)

    def handle_embed(self, context: JobContext) -> None:
        product_id = context.job.payload.get("product_id") if isinstance(context.job.payload, dict) else None
        client = self.client_factory()
        model = client.embed_model()
        with self.auth.lock, self.auth.database.session() as session:
            product = session.get(Product, product_id) if isinstance(product_id, int) else None
            if product is None:
                return
            if product.status != "active":
                self.store.delete(session, product.id)
                return
            document = self._document(session, product, self._secrets(session))
            digest = hashlib.sha256(f"{model}\0{document}".encode()).hexdigest()
            current = session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product.id))
            if current is not None and current.text_hash == digest and current.model == model:
                return
        if not model:
            raise JobFailure("向量模型尚未配置", retryable=False)
        try:
            vector = client.embed([document])[0]
        except AiCallError as error:
            raise JobFailure(error.message, retryable=False) from None
        with self.auth.lock, self.auth.database.session() as session:
            if session.get(Product, product_id) is not None:
                self.store.upsert(session, product_id, vector, digest, model)

    def _admin(self, session: Session, actor: User) -> User:
        user = session.get(User, actor.id)
        if user is None or not user.enabled or "admin" not in user.roles:
            raise AppError("forbidden", "只有管理员可以管理向量索引", 403)
        return user

    def _status(self, session: Session) -> IndexStatus:
        row = session.execute(select(ProductEmbedding.model, ProductEmbedding.dim).order_by(ProductEmbedding.updated_at.desc()).limit(1)).first()
        queued = sum(1 for _ in session.scalars(select(Job.id).where(Job.kind == "embed", Job.status.in_(["queued", "running"]))))
        return IndexStatus(backend=self.store.name, model=row[0] if row else "", dim=row[1] if row else None, queued=queued,
            products=session.scalar(select(func.count()).select_from(Product).where(Product.status == "active")) or 0,
            vectors=self.store.count(session))

    def status(self, actor: User) -> IndexStatus:
        with self.auth.lock, self.auth.database.session() as session:
            self._admin(session, actor)
            return self._status(session)

    def rebuild(self, actor: User, host: str) -> IndexStatus:
        """向量模型或维度变更后使用：清空全部向量，为每个有效商品重新派发 embed 任务。"""
        with self.auth.lock, self.auth.database.session() as session:
            admin = self._admin(session, actor)
            before = self.store.count(session)
            self.store.clear(session)
            ids = list(session.scalars(select(Product.id).where(Product.status == "active").order_by(Product.id)))
            for product_id in ids:
                self.enqueue(session, product_id)
            OplogService(session).write(admin.id, ",".join(admin.roles), "重建全部向量", "search_index", None, {"vectors": before},
                                       {"queued": len(ids), "backend": self.store.name}, "成功", host)
            return self._status(session)

    # ---- 搜索 ----
    def _candidates(self, session: Session, filters: SearchFilters, product_ids: list[int] | None = None) -> dict[int, Candidate]:
        query = select(Product.id, Product.display_name, Product.free_text, Product.project_id).join(Project, Project.id == Product.project_id).where(
            Product.status == "active", Project.status == "active")
        if filters.won is not None:
            query = query.where(Project.won.is_(filters.won))
        if filters.industry is not None:
            query = query.where(Project.industry_id == filters.industry)
        if filters.company is not None:
            query = query.where(Project.company_id == filters.company)
        if product_ids is not None:
            query = query.where(Product.id.in_(product_ids))
        rows = {row[0]: Candidate(row[0], row[1], row[2], row[3]) for row in session.execute(query)}
        tags = select(ProductTag.product_id, ProductTag.field_key, ProductTag.value)
        if product_ids is not None:
            tags = tags.where(ProductTag.product_id.in_(product_ids))
        for product_id, key, value in session.execute(tags):
            if product_id in rows:
                rows[product_id].tags[key] = value
        return rows

    def _match(self, pool: dict[int, Candidate], conditions: list[Condition], labels: dict[str, str]) -> tuple[list[Candidate], bool]:
        """路 A：字段关键词匹配。返回命中的候选（按得分）和是否放宽过。"""
        groups = [(item.field, item.label, [normalize(term) for term in item.terms if normalize(term)]) for item in conditions if item.type == "field"]
        others = [normalize(item.terms[0]) for item in conditions if item.type == "other" and normalize(item.terms[0])]
        hits: list[Candidate] = []
        for candidate in pool.values():
            name, description = candidate.name.casefold(), candidate.free_text.casefold()
            material = candidate.tags.get("material_name", "").casefold()
            score, matched = 0.0, []
            for key, label, words in groups:
                value = candidate.tags.get(key or "", "").casefold()
                if value and any(word in value for word in words):
                    # 命中的字段越多得分越高。
                    score += 1
                    matched.append(label)
                elif any(word in description or word in material or word in name for word in words):
                    score += 0.5
            for word in others:
                if word in name or word in description or word in material:
                    score += 0.5
                    matched.append("关键词")
            if score > 0:
                candidate.score, candidate.matched = score, list(dict.fromkeys(matched))
                hits.append(candidate)
        relaxed = False
        if len(hits) < self.config.relax_below:
            # 结果太少时放宽：不再区分字段，所有关键词和同义说法在全部字段与自由描述里任意包含即可。
            words = [word for _, _, group in groups for word in group] + others
            known = {candidate.product_id for candidate in hits}
            for candidate in pool.values():
                if candidate.product_id in known or not words:
                    continue
                text = candidate.haystack()
                count = sum(word in text for word in words)
                if count:
                    candidate.score, candidate.matched = 0.1 * count, ["放宽匹配"]
                    hits.append(candidate)
                    relaxed = True
        hits.sort(key=lambda item: (-item.score, item.product_id))
        return hits, relaxed

    def _understand(self, query: str, client: AiClient) -> tuple[list[Condition], bool]:
        """返回（条件, 是否来自缓存）。缓存键为「查询文本 + 字段版本」。"""
        with self.auth.lock, self.auth.database.session() as session:
            fields = [{"key": row.key, "label": row.label, "hint": row.hint} for row in session.scalars(
                select(TagField).where(TagField.enabled.is_(True)).order_by(TagField.sort_order, TagField.id))]
            version = session.scalar(select(func.max(TagField.version))) or 1
            digest = hashlib.sha256(normalize(query).encode()).hexdigest()
            cached = session.scalar(select(QueryCache).where(QueryCache.query_hash == digest, QueryCache.fields_version == version))
            if cached is not None and isinstance(cached.result, list):
                return [Condition.model_validate(item) for item in cached.result], True
        labels = {item["key"]: item["label"] for item in fields}
        conditions = parse_conditions(client.understand(query, fields), labels)
        with self.auth.lock, self.auth.database.session() as session:
            if session.scalar(select(QueryCache.id).where(QueryCache.query_hash == digest, QueryCache.fields_version == version)) is None:
                session.add(QueryCache(query_hash=digest, fields_version=version, query=query, result=[item.model_dump() for item in conditions]))
        return conditions, False

    def _by_code(self, session: Session, text: str) -> tuple[list[int], list[int]] | None:
        """像编号的输入做精确查询：返回（商品 id 列表, 项目 id 列表）；查不到返回 None。"""
        if not CODE_LIKE.fullmatch(text) or not any(char.isdigit() for char in text):
            return None
        product = session.scalar(select(Product).where(func.upper(Product.product_code) == text.upper()))
        if product is not None:
            return [product.id], [product.project_id]
        project = session.scalar(select(Project).where(func.upper(Project.project_code) == text.upper()))
        if project is not None:
            ids = list(session.scalars(select(Product.id).where(Product.project_id == project.id, Product.status == "active").order_by(Product.product_code)))
            return ids, [project.id]
        return None

    def _project_hits(self, session: Session, texts: list[str], first: list[int]) -> list[ProjectHit]:
        found: dict[int, Project] = {value: session.get(Project, value) for value in first}
        for text in texts:
            if len(found) >= 5 or len(text) < 2:
                continue
            like = f"%{text.replace('%', r'\%').replace('_', r'\_')}%"
            for project in session.scalars(select(Project).where(Project.status == "active", or_(*(column.ilike(like, escape="\\") for column in
                    (Project.project_name, Project.project_code, Project.company_name)))).order_by(Project.ingested_at.desc()).limit(5)):
                found.setdefault(project.id, project)
        return [ProjectHit(id=project.id, project_code=project.project_code, project_name=project.project_name, company_name=project.company_name,
                           won=project.won, link_id=session.scalar(select(Link.link_id).where(Link.kind == "project", Link.target_id == project.id)))
                for project in list(found.values())[:5] if project is not None]

    def _assemble(self, session: Session, ordered: list[Candidate], labels: dict[str, str]) -> list[SearchResult]:
        if not ordered:
            return []
        ids = [item.product_id for item in ordered]
        products = {row.id: row for row in session.scalars(select(Product).where(Product.id.in_(ids)))}
        projects = {row.id: row for row in session.scalars(select(Project).where(Project.id.in_({row.project_id for row in products.values()})))}
        links = {(kind, target): link for kind, target, link in session.execute(select(Link.kind, Link.target_id, Link.link_id).where(
            or_((Link.kind == "product") & Link.target_id.in_(ids), (Link.kind == "project") & Link.target_id.in_(list(projects)))))}
        relations = list(session.scalars(select(ProductRelation).where(or_(ProductRelation.set_product_id.in_(ids), ProductRelation.item_product_id.in_(ids)))))
        related_ids = {row.item_product_id for row in relations} | {row.set_product_id for row in relations}
        names = {row.id: row for row in session.scalars(select(Product).where(Product.id.in_(related_ids)))} if related_ids else {}
        results: list[SearchResult] = []
        for item in ordered:
            product = products[item.product_id]
            project = projects[product.project_id]
            others = ([row.item_product_id for row in relations if row.set_product_id == product.id] if product.kind == "set"
                      else [row.set_product_id for row in relations if row.item_product_id == product.id])
            results.append(SearchResult(id=product.id, product_code=product.product_code, display_name=product.display_name, kind=product.kind,
                tags=[ResultTag(key=key, label=labels[key], value=item.tags[key]) for key in labels if item.tags.get(key, "").strip()],
                free_text=product.free_text,
                related=sorted((RelatedProduct(id=names[value].id, product_code=names[value].product_code, display_name=names[value].display_name)
                                for value in others if value in names and names[value].status == "active"), key=lambda row: row.product_code),
                project=ResultProject(id=project.id, project_code=project.project_code, project_name=project.project_name,
                                      company_name=project.company_name, won=project.won, link_id=links.get(("project", project.id))),
                link_id=links.get(("product", product.id)), reason=item.reason, matched=item.matched, by_keywords=item.score > 0,
                by_vector=item.similarity is not None, similarity=round(item.similarity, 4) if item.similarity is not None else None))
        return results

    def search(self, body: SearchRequest, actor: User) -> SearchResponse:
        started = time.perf_counter()
        model_seconds = 0.0
        model_calls = 0
        notes: list[str] = []
        degraded = False
        query = " ".join(body.query.split())
        if not query:
            raise AppError("validation_error", "请输入要搜索的内容", 422)
        # 1) 像编号：精确查询、置顶，不调用模型。
        with self.auth.lock, self.auth.database.session() as session:
            labels = self._labels(session, enabled_only=True)
            exact = self._by_code(session, query) if body.conditions is None else None
            if exact is not None:
                pool = self._candidates(session, SearchFilters(), exact[0])
                ordered = [pool[value] for value in exact[0] if value in pool]
                for item in ordered:
                    item.reason = "编号精确匹配"
                page = ordered[(body.page - 1) * PAGE_SIZE: body.page * PAGE_SIZE]
                results = self._assemble(session, page, labels)
                hits = self._project_hits(session, [query], exact[1])
                return self._finish(session, actor, query, "code", [], False, False, [], False, 0, started, 0.0, len(ordered), body.page, results, hits)
            secrets = self._secrets(session)
            companies = [row for row in session.scalars(select(CompanyIndustryMap)) if any(name and name in query for name in (row.company_name, *row.aliases))]
        client = self.client_factory()
        safe_query = self._scrub(query, secrets).strip() or "商品"
        # 2) 查询理解与同义扩展（可缓存；页面带回条件时跳过）。
        from_cache = False
        if body.conditions is not None:
            conditions = list(body.conditions)
        else:
            conditions = [Condition(type="company", label="企业", terms=[row.company_name]) for row in companies]
            try:
                before = time.perf_counter()
                understood, from_cache = self._understand(safe_query, client)
                if not from_cache:
                    model_seconds += time.perf_counter() - before
                    model_calls += 1
                conditions += understood
            except (AiCallError, ValueError):
                degraded = True
                notes.append("查询理解暂不可用，已直接按输入的文字匹配")
                conditions += [Condition(type="other", label="关键词", terms=[word]) for word in dict.fromkeys(re.split(r"[\s，,、。]+", safe_query)) if word][:8]
        # 3) 路 B 的查询向量。
        vector: list[float] | None = None
        try:
            before = time.perf_counter()
            vector = client.embed([safe_query])[0]
            model_seconds += time.perf_counter() - before
            model_calls += 1
        except (AiCallError, ValueError, IndexError):
            degraded = True
            notes.append("向量检索暂不可用，本次只按关键词匹配")
        with self.auth.lock, self.auth.database.session() as session:
            pool = self._candidates(session, body.filters)
            named = {item.terms[0] for item in conditions if item.type == "company"}
            if named:
                allowed = set(session.scalars(select(Project.id).where(Project.company_name.in_(named))))
                company_ids = set(session.scalars(select(CompanyIndustryMap.id).where(CompanyIndustryMap.company_name.in_(named))))
                allowed |= set(session.scalars(select(Project.id).where(Project.company_id.in_(company_ids)))) if company_ids else set()
                pool = {key: value for key, value in pool.items() if value.project_id in allowed}
            excluded = [normalize(item.terms[0]) for item in conditions if item.type == "exclude" and normalize(item.terms[0])]
            if excluded:
                pool = {key: value for key, value in pool.items() if not any(word in value.haystack() for word in excluded)}
            # 4) 两路海选，合并去重。
            matched, relaxed = self._match(pool, conditions, labels)
            restricted = body.filters != SearchFilters() or bool(named) or bool(excluded)
            if vector is not None:
                found = self.store.search(session, vector, self.config.vector_top_k, set(pool) if restricted else None)
                for product_id, similarity in found:
                    if product_id in pool:
                        pool[product_id].similarity = similarity
            # 路 A 命中优先（得分高的在前），其次向量相似度。
            merged = sorted((item for item in pool.values() if item.score > 0 or item.similarity is not None),
                            key=lambda item: (-item.score, -(item.similarity or -1.0), item.product_id))[: self.config.max_candidates]
            documents = [{"index": index, "text": self._scrub(product_text(item.name, [(labels[key], item.tags[key]) for key in labels if key in item.tags], item.free_text), secrets)}
                         for index, item in enumerate(merged, 1)]
        # 5) 模型排序：只发临时序号和文字描述。
        ordered = merged
        if len(merged) > 1:
            try:
                before = time.perf_counter()
                ranking = parse_ranking(client.rank(safe_query, documents, TOP_N), len(merged))
                model_seconds += time.perf_counter() - before
                model_calls += 1
                chosen = []
                for index, reason in ranking:
                    merged[index - 1].reason = reason or None
                    chosen.append(merged[index - 1])
                picked = {item.product_id for item in chosen}
                ordered = chosen + [item for item in merged if item.product_id not in picked]
            except (AiCallError, ValueError):
                degraded = True
                notes.append("排序模型暂不可用，结果按关键词命中和相似度排列")
        with self.auth.lock, self.auth.database.session() as session:
            page = ordered[(body.page - 1) * PAGE_SIZE: body.page * PAGE_SIZE]
            results = self._assemble(session, page, labels)
            texts = [query, *named, *(item.terms[0] for item in conditions if item.type == "other")]
            hits = self._project_hits(session, texts, [])
            return self._finish(session, actor, query, "natural", conditions, relaxed, degraded, notes, from_cache, model_calls, started,
                                model_seconds, len(ordered), body.page, results, hits)

    def _finish(self, session: Session, actor: User, query: str, kind: str, conditions: list[Condition], relaxed: bool, degraded: bool,
                notes: list[str], from_cache: bool, model_calls: int, started: float, model_seconds: float, total: int, page: int,
                results: list[SearchResult], hits: list[ProjectHit]) -> SearchResponse:
        elapsed = int((time.perf_counter() - started) * 1000)
        local = max(0, elapsed - int(model_seconds * 1000))
        log = SearchLog(user_id=actor.id, query=query, kind=kind, conditions=[item.model_dump() for item in conditions], result_count=total,
                        elapsed_ms=elapsed, local_ms=local, model_calls=model_calls, degraded=degraded, clicks=[])
        session.add(log)
        session.flush()
        return SearchResponse(log_id=log.id, query=query, kind=kind, conditions=conditions, relaxed=relaxed, degraded=degraded, notes=notes,
            from_cache=from_cache, model_calls=model_calls, elapsed_ms=elapsed, local_ms=local, total=total, page=page, page_size=PAGE_SIZE,
            results=results, project_hits=hits)

    def click(self, log_id: int, product_id: int, actor: User) -> None:
        with self.auth.lock, self.auth.database.session() as session:
            log = session.get(SearchLog, log_id)
            if log is None or log.user_id != actor.id:
                raise AppError("search_log_not_found", "搜索记录不存在", 404)
            if session.get(Product, product_id) is None:
                raise AppError("product_not_found", "商品不存在", 404)
            log.clicks = [*log.clicks, product_id]
