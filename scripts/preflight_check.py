#!/usr/bin/env python3
"""上线前逐项自检，默认只做本机校验。真实网络及写入验收需显式选择。"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from getpass import getpass
import json
from pathlib import Path
import platform
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
from time import perf_counter
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from app.config import Settings, load_settings
from app.db.database import Database
from app.db.models import ProductEmbedding, Setting
from app.services.ai.client import AiClient
from app.services.ai.settings import AiSettingsService
from app.services.crypto import SecretCipher
from app.services.ingest.vectors import FORMAL_MODEL, _model_identity, _valid
from app.services.maintenance.settings import SystemParameters, daily_time
from app.services.nas import NasFs, NasNotFound, Region, safe_join
from app.services.nas.factory import create_auth_backend, create_fs, create_paths, create_system_fs
from build_release import verify


@dataclass
class Check:
    name: str
    status: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def put(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    def run(self, name: str, action: Callable[[], str]) -> None:
        try:
            self.put(name, "PASS", action())
        except Exception as exc:
            self.put(name, "FAIL", f"校验未通过（{type(exc).__name__}），请核对对应配置和部署日志")


def require(condition: bool, detail: str) -> str:
    if not condition:
        raise ValueError(detail)
    return detail


def local_database(db: Path, writable: bool) -> str:
    with closing(sqlite3.connect(db.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=1)) as connection:
        if writable:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            return "现有 SQLite 可取得写事务，未保存业务数据"
        current = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        alembic = Config()
        alembic.set_main_option("script_location", str(ROOT / "backend/alembic"))
        head = ScriptDirectory.from_config(alembic).get_current_head()
        return require(current is not None and current[0] == head, f"当前迁移版本 {head}")


def firewall() -> str:
    script = (
        "$ErrorActionPreference='Stop'; $r=Get-NetFirewallRule -Name NASDesignWorkbench-Intranet-8080 -PolicyStore ActiveStore; "
        "$p=$r | Get-NetFirewallPortFilter; $a=$r | Get-NetFirewallAddressFilter; "
        "[pscustomobject]@{on=[string]$r.Enabled; profile=[string]$r.Profile; action=[string]$r.Action; "
        "port=[string]$p.LocalPort; protocol=[string]$p.Protocol; sources=@($a.RemoteAddress)} | ConvertTo-Json -Compress"
    )
    import ipaddress
    data = json.loads(subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", script], timeout=15))
    trusted = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    valid_sources = data["sources"] and all(any(ipaddress.ip_network(cidr).subnet_of(net) for net in trusted) for cidr in data["sources"])
    return require(data["on"] == "True" and data["profile"] == "Domain, Private" and data["action"] == "Allow"
                   and data["port"] == "8080" and data["protocol"] == "TCP" and bool(valid_sources),
                   "托管入站规则生效，来源为私有 CIDR")


def sandbox_path(paths, region: Region, name: str | None) -> str:
    if not name or not name.split("/")[-1].startswith("_WP14验收"):
        raise ValueError("只能在已预先授权的 _WP14验收 目录内探测")
    return paths.join(region, name)


def write_probe(fs: NasFs, first: str, destination: str | None = None) -> str:
    token = secrets.token_hex(20)
    name = f"wp14-{token}.probe"
    source = safe_join(first, name)
    target = safe_join(destination, name) if destination else source
    if not fs.stat(first).is_dir or (destination and not fs.stat(destination).is_dir) or fs.exists(source) or (
            destination and fs.exists(target)):
        raise ValueError("授权测试目录缺失或目标冲突")
    body = token.encode()
    touched = []
    try:
        with fs.open_write(source) as stream:
            touched.append(source)
            if stream.write(body) != len(body):
                raise OSError("探测文件未完整写入")
        seconds = 0.0
        if destination:
            start = perf_counter()
            fs.rename(source, target)
            seconds = perf_counter() - start
            touched.append(target)
        with fs.open_read(target) as stream:
            if stream.read() != body:
                raise ValueError("探测文件读回失败")
        return f"同共享区 rename 并读回验证，{seconds:.3f} 秒" if destination else "授权测试目录写入及读回通过"
    finally:
        for path in touched:
            try:
                with fs.open_read(path) as stream:
                    ours = stream.read(80) == body
            except NasNotFound:
                continue
            if ours:
                fs.delete(path)


def network_checks(config: Settings, args, report: Report) -> None:
    names = ("DNS", "HTTP 8080", "DSM HTTPS", "SMB 系统账号与正式区读取", "系统账号备份区读取",
             "系统账号备份区写入", "员工临时区读写", "终审临时区与正式区同卷移动")
    if not args.network or config.mode != "prod":
        for name in names:
            report.put(name, "SKIP", "仅在正式模式且明确启用公司内网验证后执行")
    else:
        base = urlsplit(config.server.public_base_url)
        def dns() -> str:
            import ipaddress
            addresses = {ipaddress.ip_address(value[4][0]) for value in socket.getaddrinfo(
                base.hostname, 8080, family=socket.AF_INET, proto=socket.IPPROTO_TCP)}
            return require(bool(addresses) and all(ip.is_private and not ip.is_loopback for ip in addresses),
                           "固定域名解析到内网 IPv4")
        report.run("DNS", dns)
        def health() -> str:
            r = httpx.get(config.server.public_base_url.rstrip("/") + "/api/health", timeout=5, trust_env=False)
            r.raise_for_status()
            body = r.json()
            return require(body["database_available"] and body["mode"] == "prod", "已访问生产模式 HTTP 健康接口")
        if report.checks[-1].status == "PASS":
            report.run("HTTP 8080", health)
        else:
            report.put("HTTP 8080", "SKIP", "固定域名未解析到允许的内网地址")
        def dsm() -> str:
            if not config.nas.dsm_https:
                raise ValueError("生产 DSM 必须配置 HTTPS")
            context = ssl.create_default_context(cafile=str(config.nas.dsm_ca_file) if config.nas.dsm_ca_file else None)
            host = config.nas.host
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            with httpx.Client(verify=context, timeout=5, trust_env=False) as client:
                response = client.get(f"https://{host}:{config.nas.dsm_port}/webapi/query.cgi",
                    params={"api": "SYNO.API.Info", "version": 1, "method": "query", "query": "SYNO.API.Auth"})
                response.raise_for_status()
                return require(response.json().get("success") is True, "DSM HTTPS 与登录 API 发现通过")
        report.run("DSM HTTPS", dsm)
        paths = create_paths(config)
        try:
            fs = create_system_fs(config)
        except Exception as exc:
            report.put(names[3], "FAIL", f"系统账号 SMB 连接失败（{type(exc).__name__}）")
            report.put(names[4], "SKIP", "系统账号 SMB 连接失败")
            report.put(names[5], "SKIP", "系统账号 SMB 连接失败")
        else:
            try:
                report.run(names[3], lambda: (fs.list(paths.root(Region.FORMAL)), "正式区读取成功")[1])
                report.run(names[4], lambda: (fs.list(paths.root(Region.BACKUP)), "备份区读取成功")[1])
                if args.write_probe:
                    report.run(names[5], lambda: write_probe(fs, sandbox_path(paths, Region.BACKUP, args.backup_test_dir)))
                else:
                    report.put(names[5], "SKIP", "未授权备份区专用测试目录写入")
            finally:
                fs.close()
        for name, username, destination in ((names[6], args.employee, None), (names[7], args.final_reviewer, args.formal_test_dir)):
            if not args.write_probe or not username:
                report.put(name, "SKIP", "未指定已授权测试身份和专用目录")
                continue
            if not config.nas.dsm_https:
                report.put(name, "SKIP", "生产 DSM 必须配置 HTTPS")
                continue
            if username.casefold() == config.nas.system_user.casefold():
                report.put(name, "FAIL", "系统账号不能用于临时区或正式区测试")
                continue
            def person() -> str:
                password = getpass(f"{username} 密码（隐藏输入）：")
                if not create_auth_backend(config).verify(username, password):
                    raise ValueError("测试身份 DSM 登录失败")
                probe = create_fs(config, username, password)
                try:
                    return write_probe(probe, sandbox_path(paths, Region.TEMP, args.temp_test_dir),
                                       sandbox_path(paths, Region.FORMAL, destination) if name == names[7] else None)
                finally:
                    probe.close()
            report.run(name, person)
    for name, explanation in (
        ("实际 NAS ACL：系统账号禁止写正式区和访问临时区", "在群晖有效权限页核验继承和组成员。客户端限制不代表 DSM ACL。"),
        ("实际 NAS ACL：员工与管理员未获正式写权限", "在群晖按角色核对。核对管理员本人跨用户临时区清理和拒绝非授权访问。"),
        ("每设备会话和权限", "用独立浏览器会话检查本人 NAS 权限、注销和超时隔离。"),
        ("NAS 可用容量", "在 DSM 存储管理器记录卷可用空间及快照配额，覆盖预计上传和保留的历史副本。"),
        ("内网隔离", "从允许、拒绝的网段分别验证 8080，检查竞争防火墙规则、GPO、代理和公网转发。"),
        ("现有正式永久链接", "使用授权普通业务账号打开已有项目、商品、历史文件链接。记录结果，不记录 Cookie。"),
    ):
        report.put(name, "MANUAL", explanation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--package-sha")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--test-ai", action="store_true", help="显式允许向已配置的 HTTPS 百炼服务发送最小测试请求")
    parser.add_argument("--write-probe", action="store_true", help="确认已授权，仅在专用测试目录写入、移动及清理自己的探测文件")
    for option in ("employee", "final-reviewer", "temp-test-dir", "formal-test-dir", "backup-test-dir"):
        parser.add_argument("--" + option)
    parser.add_argument("--min-local-gib", type=float)
    args = parser.parse_args()
    report = Report()
    report.run("Python 3.12", lambda: require(sys.version_info[:2] == (3, 12), platform.python_version()))
    report.put("Windows", "PASS" if sys.platform == "win32" else "SKIP", platform.platform())
    if args.package and args.package_sha:
        report.run("发布包", lambda: f"{len(verify(args.package, args.package_sha)['files'])} 个文件通过 SHA-256")
    else:
        report.put("发布包", "SKIP", "需提供候选 ZIP 及独立取得的 SHA-256")
    try:
        config = load_settings(args.config)
        create_paths(config)
        report.put("配置文件", "PASS", f"{config.mode} 模式配置可解析，三区路径互不重叠")
    except Exception as exc:
        report.put("配置文件", "FAIL", f"配置不可用（{type(exc).__name__}）")
        config = None
    if config:
        report.run("固定域名 HTTP/8080", lambda: require(config.server.port == 8080 and
            urlsplit(config.server.public_base_url).scheme == "http" and urlsplit(config.server.public_base_url).port == 8080,
            "配置使用固定域名 HTTP/8080"))
        db = config.paths.data_dir / "app.db"
        report.run("SQLite 可读写", lambda: local_database(db, True))
        report.run("Alembic 当前版本", lambda: local_database(db, False))
        def key() -> str:
            cipher = SecretCipher(config.paths.data_dir, read_only=True)
            if config.mode == "prod" and not config.nas.system_password_enc.get_secret_value():
                raise ValueError("缺少系统加密配置")
            tokens = [config.nas.system_password_enc.get_secret_value(), config.ai.api_key_enc.get_secret_value()]
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
                saved = connection.execute("SELECT value FROM settings WHERE key = 'ai'").fetchone()
                if saved:
                    tokens.append(json.loads(saved[0]).get("api_key_enc"))
            for token in filter(None, tokens):
                cipher.decrypt(token)
            return "本机密钥、配置和数据库中的密文解密通过"
        report.run("配置密钥", key)
        if config.paths.data_dir.exists():
            free = shutil.disk_usage(config.paths.data_dir).free / 2**30
            report.put("本机数据磁盘空间", "MANUAL" if args.min_local_gib is None else "PASS" if free >= args.min_local_gib else "FAIL",
                       f"可用 {free:.1f} GiB。请按计划容量验收。")
        if sys.platform == "win32":
            report.run("Windows 托管防火墙规则", firewall)
        else:
            report.put("Windows 托管防火墙规则", "SKIP", "需要 Windows 主机实测")
        network_checks(config, args, report)
        runtime_checks(config, args, report)
    data = {"checks": [asdict(row) for row in report.checks]}
    for row in report.checks:
        print(f"{row.status:6} {row.name} · {row.detail}")
    if args.report:
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
    return 1 if any(row.status == "FAIL" for row in report.checks) else 0


def runtime_checks(config: Settings, args, report: Report) -> None:
    database_file = config.paths.data_dir / "app.db"
    if not database_file.exists():
        for name in ("巡检和备份调度", "正式模型与维度", "百炼识图", "百炼文本", "百炼向量"):
            report.put(name, "SKIP", "SQLite 尚未初始化")
        return
    database = Database(config.paths.data_dir)
    try:
        with database.session() as session:
            def schedule() -> str:
                row = session.scalar(select(Setting).where(Setting.key == "system_runtime"))
                if row:
                    parsed = SystemParameters.model_validate(row.value)
                    return f"已验证持久配置，Asia/Shanghai {parsed.inspection_time} 巡检，{parsed.backup_time} 备份"
                return f"已验证配置时刻，Asia/Shanghai {daily_time(config.schedule.inspection)} 巡检，{daily_time(config.schedule.backup)} 备份"
            report.run("巡检和备份调度配置", schedule)
            report.put("Windows 服务重启和夜间定时触发", "MANUAL", "重启服务后查看实际排程与最近的任务报告")
            formal = session.scalar(select(Setting).where(Setting.key == FORMAL_MODEL))
            if formal:
                def vectors() -> str:
                    value = formal.value
                    rows = list(session.scalars(select(ProductEmbedding).where(
                        ProductEmbedding.model_id == value["name"], ProductEmbedding.model_version == value["version"],
                        ProductEmbedding.index_generation == value["generation"], ProductEmbedding.state == "ready")))
                    return require(bool(rows) and all(_valid(row, row.text_hash, value["dimension"]) for row in rows),
                                   f"正式代次 {value['generation']} 的模型身份和向量维度一致")
                report.run("正式模型与维度", vectors)
            else:
                report.put("正式模型与维度", "MANUAL", "首次由真实已审批入库形成正式索引后再查验")
        names = ("百炼识图", "百炼文本", "百炼向量")
        if not args.test_ai or not args.network or config.mode != "prod":
            for name in names:
                report.put(name, "SKIP", "真实百炼测试需要生产模式、--network 和 --test-ai")
        else:
            cipher = SecretCipher(config.paths.data_dir, read_only=True)
            service = AiSettingsService(type("Context", (), {"database": database, "config": config})(), cipher)
            with database.session() as session:
                settings = service._effective(session)
            if not settings.base_url.startswith("https://"):
                raise ValueError("百炼连接必须使用 HTTPS")
            client = AiClient(settings, database, cipher)
            try:
                results = client.test_models()
                for name, row in zip(names, results, strict=True):
                    report.put(name, "PASS" if row["success"] else "FAIL", "最小模型请求通过" if row["success"] else "最小模型请求失败")
                if formal:
                    pointer = formal.value
                    profile = settings.model_copy(update={"model_embed": pointer["name"],
                                                          "model_embed_version": pointer.get("revision", "")})
                    def live_dimension() -> str:
                        if _model_identity(profile, False)[1] != pointer["version"]:
                            raise ValueError("正式向量模型身份变化")
                        official = AiClient(profile, database, cipher)
                        try:
                            size = len(official.embed("工作台连接测试", None))
                        finally:
                            official.close()
                        return require(size == pointer["dimension"], f"正式模型实际维度为 {size}")
                    report.run("真实正式 Embedding 身份与维度", live_dimension)
            finally:
                client.close()
    except Exception as exc:
        report.put("运行配置", "FAIL", f"运行配置无法验证（{type(exc).__name__}）")
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
