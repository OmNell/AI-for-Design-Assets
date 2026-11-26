"""在公开卡片柜外准备向量，并与新版本一起提交。"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EmbeddingIndex, ProductEmbedding, ProductEmbeddingHistory
from app.services.ai.client import AiCallError
from app.services.ai.orchestrator import AiService
from app.services.ingest import vectors
from app.services.jobs import JobFailure
from app.services.search.indexes import IndexService


def inputs(ai: AiService, session: Session, data: dict) -> dict[int, str]:
    if data["project"].get("status", "active") != "active":
        return {}
    active_files = {r["id"] for r in data["files"] if r["status"] == "active"}
    active = {r["id"] for r in data["products"] if r["status"] == "active" and r["file_id"] in active_files}
    return {pid: text for pid, text in vectors.project_inputs(ai, session, data).items() if pid in active}


def prepare(ai: AiService, data: dict, batch_id: int | None, prepared: dict, *,
            checkpoint: Callable[[vectors.Model, int, dict], None] | None = None) -> dict:
    with ai.auth.database.session() as session:
        formal = vectors.model_for(ai, session)
        models = [formal, *(IndexService.profile(row) for row in session.scalars(select(EmbeddingIndex).where(
            EmbeddingIndex.status.in_(("candidate", "rollback")))))]
        texts = inputs(ai, session, data)
    result = {}
    for model in models:
        key = str(model.generation)
        saved = prepared.get(f"{key}:{model.version}", prepared.get(key, {}))
        fresh: dict[str, dict] = {}
        failed = []
        with ai.auth.database.session() as session:
            dimension = vectors.dimension_for(session, model)
            missing = {pid: text for pid, text in texts.items()
                       if not vectors._valid(session.get(ProductEmbedding, model.key(pid)), vectors.text_hash(text), dimension)}
        if missing:
            try:
                client = vectors.client_for(ai, model)
                for pid, text in missing.items():
                    digest = vectors.text_hash(text)
                    prior = saved.get("items", {}).get(str(pid))
                    try:
                        reused = saved.get("version") == model.version and prior and prior["digest"] == digest
                        value = prior["vector"] if reused else client.embed(text, batch_id)
                        vectors.validate_vector(value, dimension)
                    except AiCallError:
                        failed.append(pid)
                        if model == formal:
                            raise JobFailure("正式索引向量未生成，公开版本未切换", retryable=False) from None
                        continue
                    dimension = dimension or len(value)
                    fresh[str(pid)] = {"digest": digest, "vector": value}
                    if checkpoint is not None and not reused:
                        checkpoint(model, pid, fresh[str(pid)])
            except JobFailure:
                if model == formal:
                    raise
                failed = list(missing)
        result[key] = {"name": model.name, "version": model.version, "revision": model.revision,
                       "items": fresh, "failed": failed}
    return {"formal": formal.generation, "models": result}


def publish(ai: AiService, session: Session, data: dict, batch_id: int | None, staged: dict) -> None:
    formal = vectors.model_for(ai, session)
    models = staged["models"]
    if staged["formal"] != formal.generation or models[str(formal.generation)]["version"] != formal.version:
        raise JobFailure("正式模型代次已切换，须重新准备向量")
    texts = inputs(ai, session, data)
    for generation, payload in models.items():
        row = session.get(EmbeddingIndex, int(generation))
        if int(generation) == formal.generation:
            model = formal
        elif row and row.status in ("candidate", "rollback") and row.version == payload["version"]:
            model = IndexService.profile(row)
        else:
            continue
        dimension = vectors.dimension_for(session, model)
        for pid, text in texts.items():
            existing = session.get(ProductEmbedding, model.key(pid))
            digest = vectors.text_hash(text)
            if vectors._valid(existing, digest, dimension):
                continue
            entry = payload["items"].get(str(pid))
            if batch_id is not None and existing and existing.embedding and existing.dimension:
                session.add(ProductEmbeddingHistory(batch_id=batch_id, product_id=pid, model_id=model.name,
                    model_version=model.version, index_generation=model.generation, dimension=existing.dimension,
                    embedding=existing.embedding, text_hash=existing.text_hash))
            if entry is None or entry["digest"] != digest:
                if model == formal:
                    raise JobFailure("当前版本的正式向量未齐备")
                if existing is None:
                    existing = ProductEmbedding(product_id=pid, model_id=model.name, model_version=model.version,
                                                index_generation=model.generation, text_hash=digest)
                    session.add(existing)
                existing.state, existing.text_hash = "failed", digest
                existing.error = "新版本的此索引尚未就绪"
                existing.embedding, existing.dimension = None, None
                continue
            vectors.store_vector(session, model, pid, entry["vector"], digest, dimension)
            dimension = dimension or len(entry["vector"])
    session.flush()
    if texts:
        vectors.publish_gate(ai, session, formal, texts)
