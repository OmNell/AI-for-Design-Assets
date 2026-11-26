"""本地正式向量记录；模型代次隔离留给 WP11 的索引切换。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import struct
from collections.abc import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import AiConfig
from app.db.models import CompanyIndustryMap, ProductEmbedding, ReviewSnapshot, Setting
from app.services.ai.field_catalog import FIXED_FIELDS
from app.services.ai.orchestrator import AiService
from app.services.ai.client import AiCallError
from app.services.archive import Manifest
from app.services.jobs import JobFailure

FORMAL_MODEL = "formal_embedding_model"


@dataclass(frozen=True)
class Model:
    name: str
    version: str
    generation: int = 1
    revision: str = ""

    def key(self, product_id: int) -> tuple[int, str, str, int]:
        return product_id, self.name, self.version, self.generation


def _model_identity(config: AiConfig, dev: bool) -> tuple[str, str]:
    name = config.model_embed.strip() or ("fake-embed" if dev else "")
    signature = f"{'fake' if dev else 'bailian'}|{config.base_url.rstrip('/')}|{name}|{config.model_embed_version}"
    return name, text_hash(signature)


def formal_model(ai: AiService, session: Session) -> Model | None:
    config = ai.settings._effective(session)
    formal = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL))
    if formal is None:
        return None
    revision = formal.value.get("revision")
    if revision is None:
        # WP09 指针未单独记录声明版本，优先验证原默认版本或当前部署声明。
        default = config.model_copy(update={"model_embed": formal.value["name"], "model_embed_version": ""})
        revision = "" if _model_identity(default, ai.auth.config.mode == "dev")[1] == formal.value["version"] else config.model_embed_version
    return Model(formal.value["name"], formal.value["version"], formal.value["generation"], revision)


def model_for(ai: AiService, session: Session) -> Model:
    formal = formal_model(ai, session)
    if formal is not None:
        return formal
    config = ai.settings._effective(session)
    name, version = _model_identity(config, ai.auth.config.mode == "dev")
    if not name:
        raise JobFailure("尚未配置正式向量模型", retryable=False)
    return Model(name, version, revision=config.model_embed_version)


def client_for(ai: AiService, model: Model):
    config = ai.settings.effective()
    if (config.model_embed.strip() or "fake-embed", config.model_embed_version) == (model.name, model.revision):
        client = ai.client()
    else:
        client = ai.client(config.model_copy(update={"model_embed": model.name, "model_embed_version": model.revision}))
    if _model_identity(client.config, ai.auth.config.mode == "dev") != (model.name, model.version):
        raise JobFailure("向量服务配置与索引身份不一致", retryable=False)
    return client


def project_inputs(ai: AiService, session: Session, data: Manifest) -> dict[int, str]:
    nas = ai.auth.config.nas
    project = data["project"]
    metadata = project["archive_review"] or {}
    source_company = metadata.get("source_company")
    if not source_company and metadata.get("snapshot_id") is not None:
        snapshot = session.get(ReviewSnapshot, metadata["snapshot_id"])
        source_company = snapshot.content["review"]["batch"]["company_name"] if snapshot else None
    private = [project["project_code"], nas.host, nas.share, nas.root_path.strip("/"), nas.temp_path.strip("/"),
               nas.backup_path.strip("/"), source_company or project["company_name"],
               project["company_name"], *metadata.get("actors", {}).values()]
    row = session.get(CompanyIndustryMap, project["company_id"]) if project["company_id"] is not None else None
    if row:
        private.extend([row.company_name, *row.aliases])
    return _inputs(data, private)


def _inputs(data: Manifest, sensitive: Iterable[str]) -> dict[int, str]:
    tags: dict[int, dict[str, str]] = {}
    for row in data["tags"]:
        if row["confirmation_status"] != "confirmed_value":
            continue
        value = row["value"].strip()
        if "无法判断" in value:
            value = "、".join(part.strip() for part in re.split(r"[、，,；;/]", value) if part.strip() not in ("", "无法判断"))
        if value:
            tags.setdefault(row["product_id"], {})[row["field_key"]] = value
    tokens = sorted(set(filter(None, sensitive)), key=len, reverse=True)

    def clean(text: str) -> str:
        for token in tokens:
            text = re.sub(re.escape(token), "[已省略]", text, flags=re.IGNORECASE)
        return re.sub(r"(?i)(?:https?://\S+|\\\\[^\s]+|[A-Z]:[\\/][^\s]+)", "[已省略]", text)

    result = {}
    for row in data["products"]:
        values = tags.get(row["id"], {})
        lines = [row["display_name"], *(values[key] for key in FIXED_FIELDS if key in values),
                 row["free_text"]]
        result[row["id"]] = clean("\n".join(value for value in lines if value.strip()))
    return result


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid(row: ProductEmbedding | None, digest: str, dimension: int | None) -> bool:
    if row is None or row.state != "ready" or row.text_hash != digest or not row.embedding or not row.dimension:
        return False
    if (dimension is not None and row.dimension != dimension) or len(row.embedding) != row.dimension * 4:
        return False
    return all(math.isfinite(value) for value, in struct.iter_unpack("<f", row.embedding)) and any(
        value != 0 for value, in struct.iter_unpack("<f", row.embedding))


def dimension_for(session: Session, model: Model) -> int | None:
    formal = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL))
    if formal is not None and (formal.value["name"], formal.value["version"], formal.value["generation"]) == (
        model.name, model.version, model.generation
    ):
        return formal.value["dimension"]
    return session.scalar(select(ProductEmbedding.dimension).where(ProductEmbedding.model_id == model.name,
        ProductEmbedding.model_version == model.version, ProductEmbedding.index_generation == model.generation,
        ProductEmbedding.state == "ready", ProductEmbedding.dimension.between(1, 8192),
        func.length(ProductEmbedding.embedding) == ProductEmbedding.dimension * 4).limit(1))


def validate_vector(vector: list[float], dimension: int | None) -> None:
    if (not 1 <= len(vector) <= 8192 or (dimension is not None and len(vector) != dimension)
            or not any(vector) or not all(-1e30 < value < 1e30 for value in vector)):
        raise AiCallError("向量值或维度与索引不一致")


def store_vector(session: Session, model: Model, product_id: int, vector: list[float],
                 digest: str, dimension: int | None) -> None:
    validate_vector(vector, dimension)
    row = session.get(ProductEmbedding, model.key(product_id))
    if row is None:
        row = ProductEmbedding(product_id=product_id, model_id=model.name, model_version=model.version,
                               index_generation=model.generation)
        session.add(row)
    row.text_hash, row.state, row.error = digest, "ready", None
    row.embedding, row.dimension = struct.pack(f"<{len(vector)}f", *vector), len(vector)


def generate(ai: AiService, batch_id: int | None, model: Model, inputs: dict[int, str],
             fence: Callable[[Session, int, str], bool] | None = None) -> None:
    client = client_for(ai, model)
    failures = 0
    for product_id, text in inputs.items():
        digest = text_hash(text)
        key = model.key(product_id)
        with ai.auth.database.session() as session:
            if fence is not None:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if not fence(session, product_id, digest):
                    continue
            row = session.get(ProductEmbedding, key)
            if _valid(row, digest, dimension_for(session, model)):
                continue
            if row is None:
                row = ProductEmbedding(product_id=product_id, model_id=model.name, model_version=model.version,
                                       index_generation=model.generation, text_hash=digest, state="building")
                session.add(row)
            row.text_hash, row.state, row.error = digest, "building", None
            row.attempts = (row.attempts or 0) + 1
        try:
            vector = client.embed(text, batch_id)
            with ai.auth.database.session() as session:
                if fence is not None:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if not fence(session, product_id, digest):
                        continue
                store_vector(session, model, product_id, vector, digest, dimension_for(session, model))
        except (AiCallError, OverflowError, struct.error):
            failures += 1
            with ai.auth.database.session() as session:
                if fence is not None:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if not fence(session, product_id, digest):
                        continue
                row = session.get(ProductEmbedding, key)
                row.state, row.error = "failed", "向量生成或维度校验失败"
    if failures:
        raise JobFailure(f"{failures} 个商品的正式向量未生成，请继续入库重试", retryable=False)


def publish_gate(ai: AiService, session: Session, model: Model, inputs: dict[int, str]) -> None:
    if model_for(ai, session) != model:
        raise JobFailure("正式向量索引已切换，需重新验证入库向量")
    dimension = dimension_for(session, model)
    if not inputs or not dimension or not all(
        _valid(session.get(ProductEmbedding, model.key(pid)), text_hash(text), dimension) for pid, text in inputs.items()
    ):
        raise JobFailure("正式向量校验未通过", retryable=False)
    if session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)) is None:
        session.add(Setting(key=FORMAL_MODEL, value={"name": model.name, "version": model.version,
                                                   "dimension": dimension, "generation": model.generation, "revision": model.revision}, encrypted=False))
