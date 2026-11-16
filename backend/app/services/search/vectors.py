"""向量存储：sqlite-vec 与 numpy 后备两种实现，接口与结果一致。

向量本体始终以 float32 BLOB 存在 product_embeddings 表里；sqlite-vec 的虚拟表只是它的索引，
随时可以按 BLOB 重建，所以两种实现可以互相切换而不丢数据。
"""
from __future__ import annotations

from collections.abc import Collection
import logging
import threading
from typing import Protocol

import numpy as np
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.db.database import Database
from app.db.models import ProductEmbedding

logger = logging.getLogger("nas_workbench")


def to_blob(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


class VectorStore(Protocol):
    name: str

    def upsert(self, session: Session, product_id: int, vector: list[float], text_hash: str = "", model: str = "") -> None: ...
    def delete(self, session: Session, product_id: int) -> None: ...
    def search(self, session: Session, vector: list[float], top_k: int, product_ids: Collection[int] | None = None) -> list[tuple[int, float]]: ...
    def clear(self, session: Session) -> None: ...
    def count(self, session: Session) -> int: ...


class NumpyVectorStore:
    """后备实现：查询时把全部向量读进内存，用 numpy 算余弦相似度。几千到几万个商品足够快。"""
    name = "numpy"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix: tuple[np.ndarray, np.ndarray] | None = None

    def _row(self, session: Session, product_id: int, vector: list[float], text_hash: str, model: str) -> None:
        row = session.scalar(select(ProductEmbedding).where(ProductEmbedding.product_id == product_id))
        if row is None:
            row = ProductEmbedding(product_id=product_id, text_hash=text_hash, model=model, dim=len(vector), embedding=to_blob(vector))
            session.add(row)
        else:
            row.text_hash, row.model, row.dim, row.embedding = text_hash, model, len(vector), to_blob(vector)
        session.flush()

    def upsert(self, session: Session, product_id: int, vector: list[float], text_hash: str = "", model: str = "") -> None:
        self._row(session, product_id, vector, text_hash, model)
        self._matrix = None

    def delete(self, session: Session, product_id: int) -> None:
        session.execute(delete(ProductEmbedding).where(ProductEmbedding.product_id == product_id))
        self._matrix = None

    def clear(self, session: Session) -> None:
        session.execute(delete(ProductEmbedding))
        self._matrix = None

    def count(self, session: Session) -> int:
        return len(list(session.scalars(select(ProductEmbedding.id))))

    def _load(self, session: Session, dim: int) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._matrix is None or (self._matrix[1].shape[1] if self._matrix[1].size else dim) != dim:
                rows = session.execute(select(ProductEmbedding.product_id, ProductEmbedding.embedding).where(ProductEmbedding.dim == dim)).all()
                ids = np.array([row[0] for row in rows], dtype=np.int64)
                matrix = np.vstack([from_blob(row[1]) for row in rows]) if rows else np.zeros((0, dim), dtype=np.float32)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True) if rows else np.zeros((0, 1), dtype=np.float32)
                self._matrix = (ids, matrix / np.where(norms == 0, 1, norms))
            return self._matrix

    def search(self, session: Session, vector: list[float], top_k: int, product_ids: Collection[int] | None = None) -> list[tuple[int, float]]:
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if top_k <= 0 or norm == 0:
            return []
        ids, matrix = self._load(session, len(query))
        if product_ids is not None:
            mask = np.isin(ids, np.fromiter(product_ids, dtype=np.int64, count=len(product_ids)))
            ids, matrix = ids[mask], matrix[mask]
        if not len(ids):
            return []
        scores = matrix @ (query / norm)
        # 相似度相同按商品号升序，两种实现的结果顺序一致。
        order = np.lexsort((ids, -scores))[:top_k]
        return [(int(ids[index]), float(scores[index])) for index in order]


class SqliteVecStore(NumpyVectorStore):
    """sqlite-vec 实现：KNN 在数据库里完成。限定商品集合的查询直接在该集合上计算（集合通常很小）。"""
    name = "sqlite-vec"
    TABLE = "vec_products"

    def __init__(self) -> None:
        super().__init__()
        self._dim: int | None = None

    def _table_dim(self, session: Session) -> int | None:
        sql = session.execute(text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": self.TABLE}).scalar()
        if not sql:
            return None
        return int(str(sql).split("float[")[1].split("]")[0])

    def _ensure(self, session: Session, dim: int) -> None:
        if self._dim == dim:
            return
        current = self._table_dim(session)
        if current != dim:
            # 维度变了（换了向量模型）：虚拟表按新维度重建，再从 BLOB 里把同维度的向量灌回去。
            session.execute(text(f"DROP TABLE IF EXISTS {self.TABLE}"))
            session.execute(text(f"CREATE VIRTUAL TABLE {self.TABLE} USING vec0(product_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"))
            for product_id, blob in session.execute(select(ProductEmbedding.product_id, ProductEmbedding.embedding).where(ProductEmbedding.dim == dim)).all():
                session.execute(text(f"INSERT INTO {self.TABLE}(product_id, embedding) VALUES (:id, :vector)"), {"id": product_id, "vector": blob})
        self._dim = dim

    def upsert(self, session: Session, product_id: int, vector: list[float], text_hash: str = "", model: str = "") -> None:
        self._row(session, product_id, vector, text_hash, model)
        self._ensure(session, len(vector))
        session.execute(text(f"DELETE FROM {self.TABLE} WHERE product_id = :id"), {"id": product_id})
        session.execute(text(f"INSERT INTO {self.TABLE}(product_id, embedding) VALUES (:id, :vector)"), {"id": product_id, "vector": to_blob(vector)})
        self._matrix = None

    def delete(self, session: Session, product_id: int) -> None:
        super().delete(session, product_id)
        if self._table_dim(session) is not None:
            session.execute(text(f"DELETE FROM {self.TABLE} WHERE product_id = :id"), {"id": product_id})

    def clear(self, session: Session) -> None:
        super().clear(session)
        session.execute(text(f"DROP TABLE IF EXISTS {self.TABLE}"))
        self._dim = None

    def search(self, session: Session, vector: list[float], top_k: int, product_ids: Collection[int] | None = None) -> list[tuple[int, float]]:
        if product_ids is not None:
            return super().search(session, vector, top_k, product_ids)
        if top_k <= 0 or not any(vector) or self._table_dim(session) != len(vector):
            return []
        rows = session.execute(text(f"SELECT product_id, distance FROM {self.TABLE} WHERE embedding MATCH :vector AND k = :k ORDER BY distance"),
                               {"vector": to_blob(vector), "k": top_k}).all()
        # vec0 只允许按距离排序；相似度相同的再按商品号排，与后备实现的顺序一致。
        return sorted(((int(row[0]), 1.0 - float(row[1])) for row in rows), key=lambda item: (-round(item[1], 6), item[0]))


def create_vector_store(database: Database, backend: str = "auto") -> VectorStore:
    """按配置选择实现；auto 或 sqlite-vec 加载失败时自动改用后备实现，并在日志里说明。"""
    if backend == "numpy":
        logger.info("向量检索使用后备实现（numpy），由配置指定")
        return NumpyVectorStore()
    if database.load_vector_extension():
        logger.info("向量检索使用 sqlite-vec")
        return SqliteVecStore()
    logger.warning("sqlite-vec 加载失败，向量检索自动改用后备实现（numpy）")
    return NumpyVectorStore()
