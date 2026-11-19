"""精确编号、关键词与向量混合检索。仅向模型发送授权的脱敏候选。"""
from __future__ import annotations

from collections import defaultdict
import json
import re
import time
from typing import Iterable

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.db.models import CompanyIndustryMap, Product, ProductRelation, ProductTag, Project, QueryCache, SearchClick, SearchLog, User
from app.errors import AppError
from app.schemas.catalog import ProjectFilters, ProjectRow
from app.schemas.search import ClickRequest, Conditions, SearchHit, SearchRequest, SearchResult
from app.services.ai.client import AiCallError, collect_search_usage
from app.services.ai.field_catalog import FIXED_FIELDS, fixed_fields
from app.services.ai.orchestrator import AiService
from app.services.auth.service import LoginContext
from app.services.catalog import CatalogService
from app.services.ingest import vectors
from app.services.jobs import JobFailure
from app.services.nas import NasNotFound, NasPermissionDenied, NasUnavailable
from app.services.oplog import OplogService
from .indexes import active_products, inputs_for
from .scoring import Candidate, fallback, fuse, lexical
from .store import VectorStore

CODE = re.compile(r"(?=.*\d)[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", re.ASCII)
PRIVATE_PATTERN = re.compile(
    r"(?i)(?:https?://\S+|\\\\[^\s]+|[A-Z]:[\\/][^\s]+|(?:[\w.\-\u3400-\u9fff]+[/\\]){2,}[^\s]+"
    r"|(?:password|passwd|cookie|authorization|api[_ -]?key|密码|密钥)\s*[=:：]\s*\S+"
    r"|\b(?:sk[-_][A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{28,}|Bearer\s+[A-Za-z0-9._-]+|[PH]\d{3,}(?:-\d+){0,2})\b)")


class Redactor:
    def __init__(self, values: Iterable[str]) -> None:
        literals = sorted(set(filter(None, values)), key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(value) for value in literals), re.IGNORECASE) if literals else None

    def clean(self, text: str) -> str:
        cleaned = PRIVATE_PATTERN.sub("[已省略]", text)
        return self.pattern.sub("[已省略]", cleaned) if self.pattern else cleaned


class SearchService:
    def __init__(self, ai: AiService, catalog: CatalogService, store: VectorStore) -> None:
        self.ai, self.auth, self.catalog, self.store = ai, ai.auth, catalog, store

    def _redactor(self, session: Session) -> Redactor:
        nas = self.auth.config.nas
        words = [nas.host, nas.share, nas.root_path.strip("/"), nas.temp_path.strip("/"), nas.backup_path.strip("/"), nas.system_user]
        words.extend(session.scalars(select(User.username)))
        for code, name in session.execute(select(Project.project_code, Project.company_name)):
            words.extend([code, name])
        for row in session.scalars(select(CompanyIndustryMap)):
            words.extend([row.company_name, *row.aliases])
        # 当前外部密钥若被粘贴到查询输入，也不进入日志或其它请求体。
        config = self.ai.settings._effective(session)
        encrypted = config.api_key_enc.get_secret_value()
        if encrypted:
            from app.services.crypto import CryptoError
            words.append(encrypted)
            try:
                words.append(self.ai.cipher.decrypt(encrypted))
            except CryptoError:
                pass
        for user in self.auth.config.dev_users if self.auth.config.mode == "dev" else []:
            words.append(user.password.get_secret_value())
        return Redactor(words)

    @staticmethod
    def _scrub_conditions(conditions: Conditions, redactor: Redactor) -> Conditions:
        def scrub(values: list[str]) -> list[str]:
            return list(dict.fromkeys(clean for value in values
                if (clean := redactor.clean(value).strip()) and len(clean) <= 50 and "[已省略]" not in clean and not CODE.fullmatch(clean)))
        fields = {key: terms for key, values in conditions.fields.items() if (terms := scrub(values))}
        return Conditions(fields=fields, keywords=scrub(conditions.keywords), exclude=scrub(conditions.exclude))

    def _understand(self, query: str, redactor: Redactor, warnings: list[str]) -> Conditions:
        with self.auth.database.session() as session:
            config = self.ai.settings._effective(session)
            fields = [{"key": row.key, "label": row.label, "hint": redactor.clean(row.hint), "version": row.version}
                      for row in fixed_fields(session)]
            signature = [query, config.model_text, config.base_url, config.model_text_version, fields]
            key = vectors.text_hash(json.dumps(signature, ensure_ascii=False, sort_keys=True))
            cached = session.get(QueryCache, key)
            cached_conditions = cached.conditions if cached else None
        if cached_conditions is not None:
            return self._scrub_conditions(Conditions.model_validate(cached_conditions), redactor)
        try:
            client = self.ai.client()
            if (client.config.model_text, client.config.model_text_version, client.config.base_url) != (
                config.model_text, config.model_text_version, config.base_url
            ):
                raise AiCallError("查询模型配置已变化")
            conditions = Conditions.model_validate_json(client.search_chat("query", {"query": query, "fields": fields}))
            conditions = self._scrub_conditions(conditions, redactor)
            # 排除词由用户指令或经过校验的模型输出提供。
            conditions.exclude = list(dict.fromkeys([*conditions.exclude, *fallback(query).exclude]))[:8]
        except (AiCallError, ValidationError):
            warnings.append("AI 查询解释失败，已改用关键词匹配")
            return self._scrub_conditions(fallback(query), redactor)
        with self.auth.database.session() as session:
            session.execute(insert(QueryCache).values(key=key, conditions=conditions.model_dump()).on_conflict_do_nothing())
        return conditions

    @staticmethod
    def _edited(conditions: Conditions, removed: list[str]) -> Conditions:
        ignored = set(removed)
        return Conditions(fields={key: value for key, value in conditions.fields.items() if key not in ignored},
            keywords=[value for value in conditions.keywords if f"keyword:{value}" not in ignored],
            exclude=[value for value in conditions.exclude if f"exclude:{value}" not in ignored])

    def _snapshot(self, body: SearchRequest, login: LoginContext):
        with self.auth.database.session() as session:
            self.catalog.media._check_login(session, login)
            redactor = self._redactor(session)
            query = body.query.casefold()
            exact_project = session.scalar(select(Project.id).where(func.lower(Project.project_code) == query))
            exact_product = session.scalar(select(Product.id).where(func.lower(Product.product_code) == query))
            exact = bool(exact_project or exact_product or CODE.fullmatch(body.query))
            filters = ProjectFilters(q="" if exact or len(body.query) > 120 else body.query, won=body.won,
                                     industry=body.industry, company=body.company)
            matched_projects = self.catalog.selection(session, filters)
            if exact:
                matched_projects = [row for row in matched_projects if row.id == exact_project]
            elif len(body.query) > 120:
                matched_projects = []
            filters.q = ""
            all_projects = self.catalog.selection(session, filters)
            project_map = {row.id: row for row in all_projects}
            statement = active_products()
            if exact:
                statement = statement.where(Product.id == (exact_product or -1))
            products = {row.id: row for row in session.scalars(statement) if row.project_id in project_map}
            candidates = {row.id: Candidate(row.id, row.display_name, row.free_text, {}, {}) for row in products.values()}
            ids = list(products)
            for offset in range(0, len(ids), 400):
                for tag in session.scalars(select(ProductTag).where(ProductTag.product_id.in_(ids[offset:offset + 400]),
                        ProductTag.confirmation_status == "confirmed_value")):
                    if tag.field_key in FIXED_FIELDS and tag.value.strip() and tag.value.strip() not in ("无法判断", "无法确定"):
                        candidates[tag.product_id].tags[tag.field_key] = tag.value
                        candidates[tag.product_id].terms[tag.field_key] = tag.terms
            return exact, matched_projects, products, candidates, redactor

    def _authorized(self, product: Product, login: LoginContext) -> bool:
        try:
            if self.catalog.media.main_image_id(product.id) != product.file_id:
                return False
            self.catalog.media.authorize_main_image(product.file_id, login)
            return True
        except (NasPermissionDenied, NasNotFound, NasUnavailable):
            return False
        except AppError as error:
            if error.status_code == 401:
                raise
            return False

    def _project_hits(self, rows: list[Project], login: LoginContext, host: str) -> tuple[list[ProjectRow], bool]:
        visible, denied = [], False
        for row in rows:
            try:
                self.catalog.authorize_project(row, login, host)
            except (NasPermissionDenied, NasNotFound, NasUnavailable):
                denied = True
                continue
            with self.auth.database.session() as session:
                current = session.get(Project, row.id)
                if current and current.status == "active" and row.current_path == current.current_path:
                    visible.append(self.catalog._row(session, current))
            if len(visible) >= 5:
                break
        return visible, denied

    def _vectors(self, query: str, products: dict[int, Product], warnings: list[str]):
        with self.auth.database.session() as session:
            model = vectors.formal_model(self.ai, session)
            if model is None:
                warnings.append("正式向量索引尚未建立，已使用关键词检索")
                return {}, None
            dimension = vectors.dimension_for(session, model)
            digests = {pid: vectors.text_hash(text) for pid, text in inputs_for(self.ai, session, list(products.values())).items()}
        try:
            if dimension is None:
                raise ValueError("正式向量维度未登记")
            embedding = vectors.client_for(self.ai, model).embed(query, None)
            with self.auth.database.session() as session:
                matches, _ = self.store.top(session, model, dimension, embedding, digests, len(digests))
            return {pid: score for pid, score in matches if score >= 0.45}, model.generation
        except (AiCallError, JobFailure, ValueError, TypeError, OverflowError):
            warnings.append("正式向量模型暂不可用或维度错误，已使用关键词检索")
            return {}, model.generation

    def _rank(self, ids: list[int], candidates: dict[int, Candidate], query: str, redactor: Redactor,
              warnings: list[str]) -> tuple[list[int], dict[int, str]]:
        payload = [{"index": index, "name": redactor.clean(candidates[pid].name)[:120],
                    "tags": {key: redactor.clean(value)[:100] for key, value in candidates[pid].tags.items()},
                    "description": redactor.clean(candidates[pid].description)[:300]}
                   for index, pid in enumerate(ids, 1)]
        try:
            raw = json.loads(self.ai.client().search_chat("rank", {"query": query, "candidates": payload}))
            if not isinstance(raw, dict) or set(raw) != {"items"} or not isinstance(raw["items"], list):
                raise ValueError("排序返回结构无效")
            selected: dict[int, str] = {}
            count = 0
            for row in raw["items"][:10]:
                if (not isinstance(row, dict) or set(row) != {"index", "reason"} or type(row["index"]) is not int
                        or not 1 <= row["index"] <= len(ids) or not isinstance(row["reason"], str)):
                    continue
                pid = ids[row["index"] - 1]
                if pid not in selected:
                    selected[pid] = redactor.clean(row["reason"])[:120]
                    count += 1
            if count < min(len(raw["items"]), 10):
                warnings.append("部分 AI 排序结果无效，已用本地顺序补齐")
            return [*selected, *(pid for pid in ids if pid not in selected)], selected
        except (ValueError, AiCallError):
            warnings.append("AI 排序不可用，已按关键词命中和向量相似度排序")
            return ids, {}

    def _hits(self, ids: list[int], products: dict[int, Product], candidates: dict[int, Candidate],
              lexical_scores: dict[int, tuple[float, list[str], float]], similarities: dict[int, float],
              reasons: dict[int, str], login: LoginContext, limit: int) -> list[SearchHit]:
        results = []
        with self.auth.database.session() as session:
            for pid in ids:
                product, candidate = products[pid], candidates[pid]
                if not self._authorized(product, login):
                    continue
                project = session.get(Project, product.project_id)
                left, right = ((ProductRelation.set_product_id, ProductRelation.item_product_id) if product.kind == "set"
                               else (ProductRelation.item_product_id, ProductRelation.set_product_id))
                related = list(session.scalars(active_products().where(
                    Product.project_id == project.id, Product.id.in_(select(right).where(left == pid,
                        ProductRelation.status == "active"))).order_by(Product.product_code)))
                score, fields, relaxed = lexical_scores.get(pid, (0.0, [], 0.0))
                results.append(SearchHit(id=pid, project_id=project.id, product_code=product.product_code,
                    name=product.display_name, kind=product.kind, project_name=project.project_name,
                    project_code=project.project_code, company_name=project.company_name, won=project.won,
                    project_url=self.catalog.url(self.catalog.link_id(session, "project", project.id)),
                    url=self.catalog.url(self.catalog.link_id(session, "product", pid)), tags=candidate.tags, related=[row.display_name for row in related if self._authorized(row, login)],
                    matches=fields, keyword_score=score or relaxed, similarity=similarities.get(pid),
                    reason=reasons.get(pid) or ("关键词匹配" if score or relaxed else "语义相近" if pid in similarities else "编号精确匹配")))
                if len(results) == limit:
                    break
        return results

    def search(self, body: SearchRequest, login: LoginContext, host: str) -> SearchResult:
        start = time.perf_counter()
        exact, projects, products, candidates, redactor = self._snapshot(body, login)
        query = redactor.clean(body.query)
        projects, denied = self._project_hits(projects, login, host)
        warnings: list[str] = []
        conditions, similarities, scores, reasons = Conditions(), {}, {}, {}
        generation = None
        keyword_ids, vector_ids = [], []
        with collect_search_usage() as usage:
            if exact:
                shortlist = [pid for pid, row in products.items() if self._authorized(row, login)]
                denied = denied or len(shortlist) < len(products)
                order = shortlist
            else:
                private_query = query.replace("[已省略]", " ").strip()
                if private_query:
                    conditions = self._edited(self._understand(private_query, redactor, warnings), body.remove)
                for pid, candidate in candidates.items():
                    score = lexical(candidate, conditions)
                    if score is not None:
                        scores[pid] = score
                rewritten = " ".join([*(values[0] for values in conditions.fields.values()), *conditions.keywords])
                vector_query = rewritten if body.remove else private_query
                vector_pool = {pid: row for pid, row in products.items() if pid in scores}
                if vector_query and vector_pool:
                    similarities, generation = self._vectors(vector_query, vector_pool, warnings)
                allowed: dict[int, bool] = {}

                def accessible(pid: int) -> bool:
                    nonlocal denied
                    if pid not in allowed:
                        allowed[pid] = self._authorized(products[pid], login)
                        denied = denied or not allowed[pid]
                    return allowed[pid]

                strict = sorted((pid for pid, score in scores.items() if score[0]), key=lambda pid: (-scores[pid][0], pid))
                for pid in strict:
                    if accessible(pid):
                        keyword_ids.append(pid)
                    if len(keyword_ids) == 50:
                        break
                if len(keyword_ids) < 10:
                    relaxed = sorted((pid for pid, score in scores.items() if not score[0] and score[2]),
                                     key=lambda pid: (-scores[pid][2], pid))
                    for pid in relaxed:
                        if accessible(pid):
                            keyword_ids.append(pid)
                        if len(keyword_ids) == 50:
                            break
                for pid in similarities:
                    if accessible(pid):
                        vector_ids.append(pid)
                    if len(vector_ids) == 50:
                        break
                shortlist = fuse(keyword_ids, vector_ids)
                local = sorted(shortlist, key=lambda pid: (-scores[pid][0], -scores[pid][2], -similarities.get(pid, -1), pid))
                # 召回融合约束候选集合，降级结果严格按关键词优先。
                order, reasons = self._rank(local, candidates, vector_query if body.remove else private_query, redactor, warnings) if local else ([], {})
        hits = self._hits(order, products, candidates, scores, similarities, reasons, login, body.limit)
        elapsed = round((time.perf_counter() - start) * 1000)
        with self.auth.database.session() as session:
            self.catalog.media._check_login(session, login)
            log = SearchLog(user_id=login.user.id, query=query, parsed=conditions.model_dump(), result_count=len(hits) + len(projects),
                            duration_ms=elapsed, index_generation=generation, ai_usage=usage, warnings=warnings,
                            targets={"product": [row.id for row in hits], "project": [row.id for row in projects]})
            session.add(log)
            session.flush()
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "智能检索", "search", str(log.id),
                None, {"results": log.result_count, "degraded": bool(warnings), "generation": generation}, "成功", host)
            return SearchResult(search_id=log.id, products=hits, projects=projects, conditions=conditions,
                warnings=warnings, degraded=bool(warnings), exact=exact, permission_filtered=denied, index_generation=generation,
                candidate_codes=[products[pid].product_code for pid in shortlist], keyword_candidates=len(keyword_ids),
                vector_candidates=len(vector_ids), field_labels=FIXED_FIELDS, usage=usage, elapsed_ms=elapsed)

    def click(self, body: ClickRequest, login: LoginContext, host: str) -> None:
        with self.auth.database.session() as session:
            self.catalog.media._check_login(session, login)
            log = session.get(SearchLog, body.search_id)
            if log is None or log.user_id != login.user.id or body.id not in log.targets.get(body.kind, []):
                raise AppError("search_target_missing", "找不到当前用户的检索结果", 404)
            session.add(SearchClick(search_id=log.id, user_id=login.user.id, target_kind=body.kind, target_id=body.id))
            OplogService(session).write(login.user.id, ",".join(login.user.roles), "打开检索结果", body.kind,
                                        str(body.id), None, {"search_id": log.id}, "成功", host)
