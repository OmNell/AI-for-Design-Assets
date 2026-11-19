"""安装和升级过程中的本机配置、数据库快照与恢复。"""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
import hashlib
from ipaddress import IPv4Network
import json
from pathlib import Path
import os
import shutil
import sqlite3
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from app.config import Settings, load_settings
from app.db.database import Database
from app.services.backup.format import FILES, inventory
from app.services.crypto import SecretCipher
from app.services.nas.factory import create_paths
from build_release import verify
from restore_backup import restore

import yaml

PRIVATE_NETWORKS = tuple(IPv4Network(item) for item in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def validate_networks(items: list[str]) -> None:
    if not items or any(not any(IPv4Network(item, strict=True).subnet_of(allowed) for allowed in PRIVATE_NETWORKS)
                        for item in items):
        raise ValueError("必须明确提供公司授权的私有 IPv4 CIDR")


def validate_config(source: Path, managed: Path) -> Settings:
    config = load_settings(source)
    if (config.mode != "prod" or config.server.port != 8080 or not config.server.public_base_url.startswith("http://")
            or not config.server.public_base_url.rstrip("/").endswith(":8080") or not config.bootstrap_admin
            or not config.nas.dsm_https or not config.ai.base_url.startswith("https://")
            or config.paths.data_dir != (managed / "data").resolve()):
        raise ValueError("托管部署必须使用生产配置、指定的固定域名和 data 目录")
    create_paths(config)
    key = config.paths.data_dir / "secret.key"
    if not key.is_file():
        raise ValueError("托管数据目录缺少加密密钥")
    ciphertext = config.nas.system_password_enc.get_secret_value()
    if not ciphertext or not SecretCipher(config.paths.data_dir, read_only=True).decrypt(ciphertext):
        raise ValueError("未配置可解密的系统账号密码")
    return config


def initialize(config_file: Path) -> None:
    if config_file.exists() or config_file.is_symlink():
        raise ValueError("现有配置必须通过升级或恢复流程处理")
    template = yaml.safe_load((ROOT / "backend/config.production.example.yaml").read_text(encoding="utf-8"))
    template["bootstrap_admin"] = input("初始管理员 NAS 账号：").strip()
    template["server"]["public_base_url"] = input("固定内网域名地址（http://域名:8080）：").strip()
    template["nas"]["host"] = input("群晖 DNS 名称或内网地址：").strip()
    for key, prompt in (("share", "共享名"), ("root_path", "正式区"), ("temp_path", "临时区"),
                        ("backup_path", "备份区"), ("system_user", "系统账号")):
        default = template["nas"][key]
        template["nas"][key] = input(f"{prompt}（回车使用 {default}）：").strip() or default
    ca = input("DSM 私有 CA PEM 路径（使用公共证书时直接回车）：").strip()
    if ca:
        cert = Path(ca).resolve(strict=True)
        shutil.copyfile(cert, config_file.parent / "dsm-ca.pem")
        template["nas"]["dsm_ca_file"] = "./dsm-ca.pem"
    password = getpass.getpass("wb-system 密码（隐藏输入）：")
    if not password or not template["bootstrap_admin"] or not template["nas"]["host"]:
        raise ValueError("管理员、群晖地址和系统密码必填")
    if template["bootstrap_admin"].casefold() == template["nas"]["system_user"].casefold():
        raise ValueError("工作台管理员和系统账号必须分开")
    config = Settings.model_validate(template)
    create_paths(config)
    cipher = SecretCipher(config_file.parent / "data")
    template["nas"]["system_password_enc"] = cipher.encrypt(password)
    password = ""
    fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(template, handle, allow_unicode=True, sort_keys=False)
    validate_config(config_file, config_file.parent)


def snapshot(root: Path, package: Path) -> None:
    """服务停止后制作与 WP13 恢复工具兼容的升级前备份。"""
    sources = (root / "data/app.db", root / "config.yaml", root / "data/secret.key")
    if package.exists() or not all(path.is_file() for path in sources):
        raise ValueError("无法取得升级前的三项恢复材料")
    with tempfile.TemporaryDirectory(dir=package.parent) as folder:
        db = Path(folder) / "app.db"
        with closing(sqlite3.connect(sources[0].resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as handle:
            handle.execute("VACUUM INTO ?", (str(db),))
        with ZipFile(package, "x", ZIP_DEFLATED) as out:
            manifest = {"version": 1, "identities": inventory(db), "files": {}}
            for name, path in zip(FILES, (db, *sources[1:]), strict=True):
                with path.open("rb") as reader:
                    digest = hashlib.file_digest(reader, "sha256").hexdigest()
                manifest["files"][name] = {"size": path.stat().st_size, "sha256": digest}
                out.write(path, name)
            out.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        restore(package, Path(folder) / "check")


def rollback(root: Path, package: Path) -> None:
    """校验旧库后替换三个托管文件，并保留失败升级的文件。"""
    with tempfile.TemporaryDirectory(dir=root) as temp:
        temp = Path(temp)
        verified = temp / "verified"
        expected = restore(package, verified)
        evidence = Path(tempfile.mkdtemp(prefix="failed-upgrade-", dir=root / "backups"))
        mapping = ("data/app.db", "data/app.db-wal", "data/app.db-shm", "config.yaml", "data/secret.key")
        moved, installed = [], []
        try:
            for rel in mapping:
                source = root / rel
                if source.exists():
                    destination = evidence / source.name
                    source.replace(destination)
                    moved.append((destination, source))
            for rel in ("data/app.db", "config.yaml", "data/secret.key"):
                target = root / rel
                (verified / rel).replace(target)
                installed.append(target)
            if inventory(root / "data/app.db") != expected:
                raise ValueError("回滚后卡片柜身份校验失败")
        except BaseException:
            for target in installed:
                target.unlink()
            for source, target in reversed(moved):
                source.replace(target)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("extract", "initialize", "migrate", "snapshot", "rollback", "networks"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--sha")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--service-stopped", action="store_true")
    parser.add_argument("--cidr", nargs="+")
    args = parser.parse_args()
    try:
        if args.action == "networks":
            validate_networks(args.cidr)
            return 0
        if args.action == "extract":
            if not args.sha:
                parser.error("安装和升级必须提供可信渠道取得的发布 SHA-256")
            data = verify(args.package, args.sha, args.target)
            print(data["version"])
            return 0
        root = args.root.resolve(strict=True)
        if args.action == "initialize":
            initialize(root / "config.yaml")
        elif args.action == "migrate":
            config = validate_config(root / "config.yaml", root)
            db = Database(config.paths.data_dir)
            try:
                db.migrate()
                if not db.is_available():
                    raise ValueError("数据库迁移后无法读取")
            finally:
                db.close()
        elif args.action in {"snapshot", "rollback"}:
            if not args.service_stopped:
                parser.error("必须先停止 Windows 服务并确认退出")
            (snapshot if args.action == "snapshot" else rollback)(root, args.package)
        return 0
    except (ValueError, OSError, sqlite3.DatabaseError, RuntimeError) as exc:
        print(f"本机维护步骤失败（{type(exc).__name__}），请核对配置、路径与日志。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
