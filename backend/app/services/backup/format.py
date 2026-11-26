"""备份格式、内容指纹与 SQLite 完整性核验，供备份服务和恢复工具共用。"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import BinaryIO
from zipfile import ZipFile

FILES = ("app.db", "config.yaml", "secret.key")
IDENTITY_TABLES = {
    "projects": "id", "products": "id", "files": "id", "links": "kind,target_id,link_id",
    "product_embeddings": "product_id,model_id,model_version,index_generation,text_hash,dimension,embedding",
}
BLOCK = 1024 * 1024
BACKUP_NAME = re.compile(r"wb-backup-\d{8}-\d{4}\.zip")


class BackupFormatError(ValueError):
    """备份归档结构与数据清单不一致。"""


def read_manifest(archive: ZipFile) -> dict:
    entries = archive.infolist()
    if (len(entries) != len(FILES) + 1 or {entry.filename for entry in entries} != {*FILES, "manifest.json"}
            or any(entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 for entry in entries)):
        raise BackupFormatError("归档只能包含工作台规定的普通文件")
    if archive.getinfo("manifest.json").file_size > 64 * 1024:
        raise BackupFormatError("备份清单大小不合法")
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (UnicodeError, json.JSONDecodeError):
        raise BackupFormatError("备份清单无法读取") from None
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1
            or not isinstance(manifest.get("identities"), dict)
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != set(FILES)):
        raise BackupFormatError("备份版本或清单不合法")
    for name in FILES:
        expected = manifest["files"][name]
        if (not isinstance(expected, dict) or type(expected.get("size")) is not int
                or expected["size"] != archive.getinfo(name).file_size or expected["size"] <= 0
                or not isinstance(expected.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", expected["sha256"])):
            raise BackupFormatError("备份文件大小或指纹与清单不一致")
    return manifest


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
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
        number = int(revision[0].split("_", 1)[0]) if revision else 0
        for table, columns in IDENTITY_TABLES.items():
            if table != "projects" and 0 < number < 16:
                # 正式资产卡片柜在 0016 建立，升级前快照也需要可核验。
                continue
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
