#!/usr/bin/env python3
"""将工作台备份验证并恢复到新的隔离目录。"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
import tempfile
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.backup.format import FILES, inventory, measure


def restore(package: Path, target: Path) -> dict:
    if target.exists() or target.is_symlink():
        raise ValueError("目标目录必须不存在，禁止覆盖已运行或已有的工作台")
    with ZipFile(package) as archive:
        entries = archive.infolist()
        if (len(entries) != len(FILES) + 1 or {entry.filename for entry in entries} != {*FILES, "manifest.json"}
                or any(entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 for entry in entries)):
            raise ValueError("归档只能包含工作台规定的普通文件")
        if archive.getinfo("manifest.json").file_size > 64 * 1024:
            raise ValueError("备份清单大小不合法")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["version"] != 1 or set(manifest["files"]) != set(FILES):
            raise ValueError("备份版本或清单不合法")
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
        try:
            data = target / "data"
            data.mkdir(mode=0o700)
            for name in FILES:
                expected = manifest["files"][name]
                entry = archive.getinfo(name)
                if expected["size"] != entry.file_size or entry.file_size == 0:
                    raise ValueError("备份文件大小与清单不一致")
                destination = (target if name == "config.yaml" else data) / name
                with archive.open(entry) as reader, destination.open("xb") as writer:
                    destination.chmod(0o600)
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                with destination.open("rb") as stream:
                    if measure(stream) != (expected["size"], expected["sha256"]):
                        raise ValueError("备份成员指纹与清单不一致")
            from app.config import load_settings
            from cryptography.fernet import Fernet
            load_settings(target / "config.yaml")
            Fernet((data / "secret.key").read_bytes())
            identities = inventory(data / "app.db")
            if manifest["identities"] != identities:
                raise ValueError("项目、商品、文件、永久链接或向量的恢复结果不一致")
            return identities
        except BaseException:
            shutil.rmtree(target)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="备份 ZIP 文件")
    parser.add_argument("--target", type=Path, help="新的恢复目录，默认使用系统临时位置")
    parser.add_argument("--service-stopped", action="store_true", help="确认目标工作台服务已停止")
    args = parser.parse_args()
    if not args.service_stopped:
        parser.error("恢复前请确认目标工作台已停止，并使用 --service-stopped")
    target = args.target or Path(tempfile.mkdtemp(prefix="wb-restore-")) / "workbench"
    try:
        identities = restore(args.package, target)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"恢复失败（{type(exc).__name__}），请检查归档和目标目录。\n")
    print(f"恢复验证通过：{target.resolve()}")
    print(json.dumps({table: info["count"] for table, info in identities.items()}, ensure_ascii=False))
    print("启动隔离实例前，请将 NASWB_PATHS__DATA_DIR 指向该目录下的 data，核对配置中的网络和 NAS 环境。")


if __name__ == "__main__":
    main()
