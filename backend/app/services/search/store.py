"""同一份 SQLite 向量 BLOB，优先 sqlite-vec，无法加载时使用 NumPy。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
import math
import sqlite3

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app.db.models import ProductEmbedding
from app.services.ingest.vectors import Model, _valid, store_vector

logger = logging.getLogger("nas_workbench")


class VectorStore:
    def __init__(self, prefer_sqlite: bool = True) -> None:
        self.prefer_sqlite = prefer_sqlite

    def backend(self, session: Session) -> str:
        if not self.prefer_sqlite:
            return "numpy"
        raw = session.connection().connection.driver_connection
        try:
            raw.execute("SELECT vec_version()").close()
            return "sqlite-vec"
        except sqlite3.Error:
            pass
        try:
            import sqlite_vec
            raw.enable_load_extension(True)
            try:
                sqlite_vec.load(raw)
            finally:
                raw.enable_load_extension(False)
        except (ImportError, AttributeError, OSError, sqlite3.Error):
            return "numpy"
        return "sqlite-vec"

    @staticmethod
    def get(session: Session, product_id: int, model: Model, digest: str, dimension: int) -> ProductEmbedding | None:
        row = session.get(ProductEmbedding, model.key(product_id))
        return row if _valid(row, digest, dimension) else None

    @staticmethod
    def upsert(session: Session, model: Model, product_id: int, vector: list[float], digest: str, dimension: int) -> None:
        store_vector(session, model, product_id, vector, digest, dimension)

    @staticmethod
    def _query(model: Model):
        return select(ProductEmbedding).where(ProductEmbedding.model_id == model.name,
            ProductEmbedding.model_version == model.version, ProductEmbedding.index_generation == model.generation,
            ProductEmbedding.state == "ready")

    def top(self, session: Session, model: Model, dimension: int, vector: Sequence[float],
            digests: Mapping[int, str], limit: int = 50) -> tuple[list[tuple[int, float]], str]:
        query = np.asarray(vector, dtype="<f4")
        if query.shape != (dimension,) or not np.isfinite(query).all() or not np.any(query):
            raise ValueError("查询向量维度或数值无效")
        backend = self.backend(session)
        scores: list[tuple[int, float]] = []
        targets = list(digests)
        for offset in range(0, len(targets), 400):
            rows = list(session.scalars(self._query(model).where(ProductEmbedding.product_id.in_(targets[offset:offset + 400]),
                ProductEmbedding.dimension == dimension)))
            rows = [row for row in rows if _valid(row, digests[row.product_id], dimension)]
            if not rows:
                continue
            if backend == "sqlite-vec":
                try:
                    values = session.execute(select(ProductEmbedding.product_id,
                        func.vec_distance_cosine(ProductEmbedding.embedding, query.tobytes())).where(
                        ProductEmbedding.model_id == model.name, ProductEmbedding.model_version == model.version,
                        ProductEmbedding.index_generation == model.generation,
                        ProductEmbedding.product_id.in_([row.product_id for row in rows])))
                    scores.extend((pid, 1 - distance) for pid, distance in values if distance is not None and math.isfinite(distance))
                    continue
                except DBAPIError:
                    logger.warning("sqlite-vec 查询不可用，回退 NumPy")
                    backend = "numpy"
            matrix = np.stack([np.frombuffer(row.embedding, dtype="<f4") for row in rows]).astype(np.float64)
            query64 = query.astype(np.float64)
            similarity = (matrix @ query64) / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(query64))
            scores.extend((row.product_id, float(value)) for row, value in zip(rows, similarity))
        scores = [(pid, round(min(1.0, max(-1.0, score)), 6)) for pid, score in scores]
        return sorted(scores, key=lambda row: (-row[1], row[0]))[:limit], backend
