"""本地正式向量记录；模型代次隔离留给 WP11 的索引切换。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import struct
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AiConfig
from app.db.models import ProductEmbedding, Setting
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

    def key(self, product_id: int) -> tuple[int, str, str, int]:
        return product_id, self.name, self.version, self.generation


def _model_identity(config: AiConfig, dev: bool) -> tuple[str, str]:
    name = config.model_embed.strip() or ("fake-embed" if dev else "")
    signature = f"{'fake' if dev else 'bailian'}|{config.base_url.rstrip('/')}|{name}|{config.model_embed_version}"
    return name, text_hash(signature)


def model_for(ai: AiService, session: Session) -> Model:
    config = ai.settings._effective(session)
    dev = ai.auth.config.mode == "dev"
    name, version = _model_identity(config, dev)
    if not name:
        raise JobFailure("尚未配置正式向量模型", retryable=False)
    formal = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL))
    if formal and (formal.value["name"], formal.value["version"]) != (name, version):
        raise JobFailure("向量模型设置与当前正式索引不同，需先完成索引迁移", retryable=False)
    return Model(name, version, formal.value["generation"] if formal else 1)


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


def _dimension(session: Session, model: Model) -> int | None:
    formal = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL))
    if formal is not None:
        return formal.value["dimension"]
    return session.scalar(select(ProductEmbedding.dimension).where(ProductEmbedding.model_id == model.name,
        ProductEmbedding.model_version == model.version, ProductEmbedding.index_generation == model.generation,
        ProductEmbedding.state == "ready").limit(1))


def generate(ai: AiService, batch_id: int, model: Model, inputs: dict[int, str]) -> None:
    client = ai.client()
    # 配置可能在客户端取用前修改；条件不符时由终审重新开始门禁。
    if _model_identity(client.config, ai.auth.config.mode == "dev") != (model.name, model.version):
        raise JobFailure("向量模型配置已变化，请重新继续入库", retryable=False)
    failures = 0
    for product_id, text in inputs.items():
        digest = text_hash(text)
        key = model.key(product_id)
        with ai.auth.database.session() as session:
            row = session.get(ProductEmbedding, key)
            if _valid(row, digest, _dimension(session, model)):
                continue
            if row is None:
                row = ProductEmbedding(product_id=product_id, model_id=model.name, model_version=model.version,
                                       index_generation=model.generation, text_hash=digest, state="building")
                session.add(row)
            row.text_hash, row.state, row.error = digest, "building", None
            row.attempts = (row.attempts or 0) + 1
        try:
            vector = client.embed(text, batch_id)
            binary = struct.pack(f"<{len(vector)}f", *vector)
            if not vector or not any(vector) or not all(math.isfinite(value) for value in vector):
                raise AiCallError("向量值不合法")
            with ai.auth.database.session() as session:
                dimension = _dimension(session, model)
                if dimension is not None and len(vector) != dimension:
                    raise AiCallError("向量维度与正式索引不一致")
                row = session.get(ProductEmbedding, key)
                row.state, row.embedding, row.dimension = "ready", binary, len(vector)
        except (AiCallError, OverflowError, struct.error):
            failures += 1
            with ai.auth.database.session() as session:
                row = session.get(ProductEmbedding, key)
                row.state, row.error = "failed", "向量生成或维度校验失败"
    if failures:
        raise JobFailure(f"{failures} 个商品的正式向量未生成，请继续入库重试", retryable=False)


def publish_gate(ai: AiService, session: Session, model: Model, inputs: dict[int, str]) -> None:
    if model_for(ai, session) != model:
        raise JobFailure("向量模型配置已变化，请重新继续入库", retryable=False)
    dimension = _dimension(session, model)
    if not inputs or not dimension or not all(
        _valid(session.get(ProductEmbedding, model.key(pid)), text_hash(text), dimension) for pid, text in inputs.items()
    ):
        raise JobFailure("正式向量校验未通过", retryable=False)
    if session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL)) is None:
        session.add(Setting(key=FORMAL_MODEL, value={"name": model.name, "version": model.version,
                                                   "dimension": dimension, "generation": model.generation}, encrypted=False))
