"""正式与候选向量代次管理。保留旧 BLOB，原子切换正式指针。"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CatalogFile, EmbeddingIndex, Job, Product, ProductEmbedding, ProductTag, Project, Setting
from app.errors import AppError
from app.schemas.search import BuildIndex, IndexInfo, IndexStatus
from app.services.ai.orchestrator import AiService
from app.services.ai.settings import require_admin
from app.services.auth.service import LoginContext
from app.services.ingest import vectors
from app.services.jobs import JobContext, JobFailure
from app.services.oplog import OplogService
from app.services.upload.service import UploadService
from .store import VectorStore

INDEX_JOB = "embed_index"


def active_products():
    return select(Product).join(Project, Project.id == Product.project_id).join(CatalogFile, CatalogFile.id == Product.file_id).where(
        Project.status == "active", Product.status == "active", CatalogFile.status == "active",
        CatalogFile.project_id == Project.id)


def inputs_for(ai: AiService, session: Session, products: list[Product]) -> dict[int, str]:
    inputs: dict[int, str] = {}
    by_project: dict[int, list[Product]] = defaultdict(list)
    for product in products:
        by_project[product.project_id].append(product)
    tags: dict[int, list[dict[str, object]]] = defaultdict(list)
    ids = [p.id for p in products]
    for offset in range(0, len(ids), 400):
        for row in session.scalars(select(ProductTag).where(ProductTag.product_id.in_(ids[offset:offset + 400]))):
            tags[row.product_id].append({"product_id": row.product_id, "field_key": row.field_key,
                "value": row.value, "confirmation_status": row.confirmation_status})
    for project_id, rows in by_project.items():
        project = session.get(Project, project_id)
        data = {"project": {"project_code": project.project_code, "folder_name": project.folder_name,
                "company_name": project.company_name, "company_id": project.company_id, "archive_review": project.archive_review},
                "products": [{"id": row.id, "display_name": row.display_name, "free_text": row.free_text} for row in rows],
                "tags": [tag for row in rows for tag in tags[row.id]]}
        inputs.update(vectors.project_inputs(ai, session, data))
    return inputs


class IndexService:
    def __init__(self, ai: AiService, store: VectorStore) -> None:
        self.ai, self.store, self.auth = ai, store, ai.auth

    @staticmethod
    def profile(index: EmbeddingIndex) -> vectors.Model:
        return vectors.Model(index.name, index.version, index.generation, index.revision)

    def _formal_row(self, session: Session) -> EmbeddingIndex | None:
        model = vectors.formal_model(self.ai, session)
        if model is None:
            return None
        row = session.get(EmbeddingIndex, model.generation)
        if row is None:
            row = EmbeddingIndex(generation=model.generation, name=model.name, version=model.version,
                                 revision=model.revision, status="formal")
        return row

    @staticmethod
    def _job_query(generation: int):
        return select(Job).where(Job.kind == INDEX_JOB, func.json_extract(Job.payload, "$.generation") == generation)

    def _schedule(self, session: Session, generation: int) -> None:
        if session.scalar(self._job_query(generation).where(Job.status == "queued").limit(1)) is None:
            self.ai.queue.enqueue(INDEX_JOB, {"generation": generation}, session=session)

    def sync_after_publish(self, session: Session) -> None:
        """入库发布只登记候选任务，候选失败不会改变正式发布条件。"""
        for generation in session.scalars(select(EmbeddingIndex.generation).where(
            EmbeddingIndex.status.in_(("candidate", "rollback")))):
            self._schedule(session, generation)

    def _status(self, session: Session) -> IndexStatus:
        rows = list(session.scalars(select(EmbeddingIndex).where(EmbeddingIndex.status != "archived").order_by(EmbeddingIndex.generation)))
        formal = self._formal_row(session)
        if formal is not None and all(row.generation != formal.generation for row in rows):
            rows.insert(0, formal)
        inputs = {pid: vectors.text_hash(text) for pid, text in inputs_for(
            self.ai, session, list(session.scalars(active_products()))).items()}
        summaries = []
        for row in rows:
            info = self._coverage(session, row, inputs)
            summaries.append(info)
        return IndexStatus(backend=self.store.backend(session), indexes=summaries)

    def _coverage(self, session: Session, row: EmbeddingIndex, inputs: dict[int, str]) -> IndexInfo:
        model = self.profile(row)
        dimension = vectors.dimension_for(session, model)
        ready, failures = 0, 0
        for pid, digest in inputs.items():
            value = session.get(ProductEmbedding, model.key(pid))
            ready += int(dimension is not None and vectors._valid(value, digest, dimension))
            failures += int(value is not None and value.text_hash == digest and value.state == "failed")
        jobs = self._job_query(row.generation).order_by(Job.id.desc())
        running = session.scalar(jobs.where(Job.status.in_(("queued", "running"))).limit(1)) is not None
        latest = session.scalar(jobs.limit(1))
        if latest and latest.status == "failed" and failures == 0 and ready < len(inputs):
            failures = len(inputs) - ready
        return IndexInfo(generation=row.generation, name=row.name, version=row.version, revision=row.revision,
            status=row.status, dimension=dimension, total=len(inputs), ready=ready, failures=failures,
            pending=len(inputs) - ready - failures, verified=bool(dimension and ready == len(inputs)), running=running)

    def read(self, login: LoginContext) -> IndexStatus:
        with self.auth.database.session() as session:
            UploadService._check_login(session, login)
            require_admin(session, login.user)
            return self._status(session)

    def create(self, data: BuildIndex, login: LoginContext, host: str) -> IndexStatus:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            require_admin(session, login.user)
            formal = self._formal_row(session)
            if formal is None:
                raise AppError("formal_index_missing", "请先完成首批正式入库，建立正式索引", 409)
            if session.scalar(select(EmbeddingIndex.generation).where(EmbeddingIndex.status == "candidate")) is not None:
                raise AppError("candidate_exists", "已有候选索引，请重试或取消该候选", 409)
            config = self.ai.settings._effective(session).model_copy(update={
                "model_embed": data.name, "model_embed_version": data.revision})
            name, version = vectors._model_identity(config, self.auth.config.mode == "dev")
            if (name, version) == (formal.name, formal.version):
                raise AppError("same_model", "候选模型或版本须与正式索引不同", 422)
            index_max = session.scalar(select(func.max(EmbeddingIndex.generation))) or 0
            vector_max = session.scalar(select(func.max(ProductEmbedding.index_generation))) or 0
            generation = max(formal.generation, index_max, vector_max) + 1
            if session.get(EmbeddingIndex, formal.generation) is None:
                session.add(formal)
                setting = session.scalar(select(Setting).where(Setting.key == vectors.FORMAL_MODEL))
                setting.value = {**setting.value, "revision": formal.revision}
            session.add(EmbeddingIndex(generation=generation, name=name, version=version, revision=data.revision, status="candidate"))
            self._schedule(session, generation)
            self._log(session, login.user.id, "建立候选向量索引", generation, host, {"model": name, "revision": data.revision})
            return self._status(session)

    def action(self, generation: int, action: str, login: LoginContext, host: str) -> IndexStatus:
        with self.auth.lock, self.auth.database.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            UploadService._check_login(session, login)
            require_admin(session, login.user)
            row = session.get(EmbeddingIndex, generation)
            if row is None or row.status not in ("candidate", "rollback"):
                raise AppError("index_missing", "该索引不在候选或回滚状态", 404)
            if action == "retry":
                self._schedule(session, generation)
                self._log(session, login.user.id, "重试向量索引", generation, host)
            elif action == "cancel" and row.status == "candidate":
                row.status = "archived"
                self._log(session, login.user.id, "取消候选向量索引", generation, host)
            elif (action, row.status) in (("switch", "candidate"), ("rollback", "rollback")):
                self._switch(session, row, login.user.id, host, action)
            else:
                raise AppError("invalid_index_action", "当前索引状态不支持该操作", 409)
            return self._status(session)

    def _switch(self, session: Session, row: EmbeddingIndex, actor_id: int, host: str, action: str) -> None:
        items = inputs_for(self.ai, session, list(session.scalars(active_products())))
        state = self._coverage(session, row, {pid: vectors.text_hash(text) for pid, text in items.items()})
        if not state.verified or state.running:
            raise AppError("index_incomplete", "索引未完成完整性校验，暂不能切换", 409, state.model_dump())
        config = self.ai.settings._effective(session).model_copy(update={"model_embed": row.name, "model_embed_version": row.revision})
        if vectors._model_identity(config, self.auth.config.mode == "dev") != (row.name, row.version):
            raise AppError("index_provider_changed", "向量服务配置已变化，无法使用该索引", 409)
        formal = self._formal_row(session)
        if formal is None:
            raise AppError("formal_index_missing", "未找到正式索引", 409)
        for old in session.scalars(select(EmbeddingIndex).where(EmbeddingIndex.status == "rollback", EmbeddingIndex.generation != row.generation)):
            old.status = "archived"
        formal.status, row.status = "rollback", "formal"
        setting = session.scalar(select(Setting).where(Setting.key == vectors.FORMAL_MODEL))
        setting.value = {"name": row.name, "version": row.version, "revision": row.revision,
                         "generation": row.generation, "dimension": state.dimension}
        self._log(session, actor_id, "回滚正式向量索引" if action == "rollback" else "切换正式向量索引",
                  row.generation, host, {"previous_generation": formal.generation})

    def handle(self, context: JobContext) -> None:
        generation = context.job.payload["generation"]
        with self.auth.database.session() as session:
            row = session.get(EmbeddingIndex, generation)
            if row is None or row.status not in ("candidate", "rollback"):
                return
            model = self.profile(row)
            products = list(session.scalars(active_products().order_by(Product.id)))
            inputs = inputs_for(self.ai, session, products)
        def still_current(session: Session, product_id: int, digest: str) -> bool:
            index = session.get(EmbeddingIndex, generation)
            if index is None or index.status not in ("candidate", "rollback") or self.profile(index) != model:
                return False
            product = session.scalar(active_products().where(Product.id == product_id))
            text = inputs_for(self.ai, session, [product]).get(product_id) if product else None
            return text is not None and vectors.text_hash(text) == digest

        vectors.generate(self.ai, None, model, inputs, fence=still_current)

    @staticmethod
    def _log(session: Session, user_id: int, action: str, generation: int, host: str,
             after: dict[str, object] | None = None) -> None:
        OplogService(session).write(user_id, "admin", action, "embedding_index", str(generation), None,
                                    after or {"generation": generation}, "成功", host)
