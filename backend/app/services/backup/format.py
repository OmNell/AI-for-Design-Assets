"""备份格式、内容指纹与 SQLite 完整性核验，供备份服务和恢复工具共用。"""
from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import BinaryIO

FILES = ("app.db", "config.yaml", "secret.key")
IDENTITY_TABLES = {
    "projects": "id", "products": "id", "files": "id", "links": "kind,target_id,link_id",
    "product_embeddings": "product_id,model_id,model_version,index_generation,text_hash,dimension,embedding",
}
BLOCK = 1024 * 1024


def measure(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(BLOCK):
        digest.update(block)
        size += len(block)
    return size, digest.hexdigest()


def inventory(db_path: Path) -> dict[str, dict[str, int | str]]:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("数据库完整性或外键检查失败")
        data = {}
        for table, columns in IDENTITY_TABLES.items():
            select = db.execute(f"SELECT {columns} FROM {table} ORDER BY {columns}")
            count = 0
            digest = hashlib.sha256()
            for row in select:
                count += 1
                for value in row:
                    raw = value if isinstance(value, bytes) else repr(value).encode("utf-8")
                    digest.update(len(raw).to_bytes(8, "big"))
                    digest.update(raw)
            data[table] = {"count": count, "sha256": digest.hexdigest()}
        return data
