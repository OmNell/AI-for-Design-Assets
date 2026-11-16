#!/usr/bin/env python3
"""WP00 独立探路；仅操作显式指定的测试目录，不依赖正式工程。"""
from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import logging
import ntpath
import re
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import BinaryIO, Callable, Protocol, TypeVar, cast
from urllib.parse import quote, quote_plus

import httpx
import smbclient
from smbprotocol.session import Session

TOTAL_SIZE = 20 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
T = TypeVar("T")


class NasFs(Protocol):
    """探路用最小接口，所有远程文件操作都经当前账号的实例执行。"""
    def list(self, path: str) -> list[str]: ...
    def mkdir(self, path: str) -> None: ...
    def open(self, path: str, mode: str) -> BinaryIO: ...
    def rename(self, source: str, target: str) -> None: ...
    def delete(self, path: str, directory: bool = False) -> None: ...


class SmbNasFs:
    def __init__(self, host: str, share: str, username: str, timeout: float) -> None:
        self.host = host
        self.share = share
        self.username = username
        self.timeout = timeout
        # 不能使用 smbclient 的全局缓存：不同账号必须有不同 TCP 连接。
        self.cache: dict[str, object] = {}
        self.session: Session | None = None

    def connect(self, password: str) -> str:
        self.session = smbclient.register_session(
            self.host, username=self.username, password=password,
            connection_cache=self.cache, connection_timeout=self.timeout,
            auth_protocol="ntlm", require_signing=True,
        )
        self.session.password = None
        return "已建立独立 SMB 会话（NTLM，要求签名，不挂盘）"

    def options(self) -> dict[str, object]:
        if self.session is None or not self.session.connection.transport.connected:
            raise ConnectionError("SMB 会话未连接或已断开；不自动换账号或重连")
        return {"username": self.username, "connection_cache": self.cache,
                "connection_timeout": self.timeout, "auth_protocol": "ntlm"}

    def path(self, relative: str) -> str:
        return ntpath.join(f"\\\\{self.host}\\{self.share}", relative_path(relative))

    def list(self, path: str) -> list[str]:
        return smbclient.listdir(self.path(path), **self.options())

    def mkdir(self, path: str) -> None:
        smbclient.mkdir(self.path(path), **self.options())

    def open(self, path: str, mode: str) -> BinaryIO:
        return cast(BinaryIO, smbclient.open_file(self.path(path), mode=mode, **self.options()))

    def rename(self, source: str, target: str) -> None:
        # 只调用服务端 rename，不使用 move/copy 或跨共享文件夹降级。
        smbclient.rename(self.path(source), self.path(target), **self.options())

    def delete(self, path: str, directory: bool = False) -> None:
        operation = smbclient.rmdir if directory else smbclient.remove
        operation(self.path(path), **self.options())

    def close(self) -> str:
        smbclient.reset_connection_cache(connection_cache=self.cache, fail_on_error=True)
        self.session = None
        return "连接已关闭"


class DsmError(RuntimeError):
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        super().__init__(json.dumps(response, ensure_ascii=False))


class DsmClient:
    def __init__(self, host: str, port: int, https: bool, verify: bool | str,
                 timeout: float, remember: Callable[[str], None]) -> None:
        self.client = httpx.Client(
            base_url=f"{'https' if https else 'http'}://{host}:{port}/webapi/",
            verify=verify, timeout=timeout, trust_env=False, follow_redirects=False,
        )
        self.sid: str | None = None
        self.remember = remember
        self.auth_path = "entry.cgi"
        self.auth_version = "6"

    def request(self, path: str, data: dict[str, str]) -> dict[str, object]:
        response = self.client.post(path, data=data)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("DSM 响应不是 JSON 对象")
        if payload.get("success") is not True:
            raise DsmError(payload)
        return payload

    def login(self, username: str, password: str) -> str:
        info = self.request("query.cgi", {
            "api": "SYNO.API.Info", "version": "1", "method": "query", "query": "SYNO.API.Auth",
        })
        auth = info["data"]["SYNO.API.Auth"]
        path = auth["path"]
        if not re.fullmatch(r"[A-Za-z0-9_/]+\.cgi", path) or ".." in path:
            raise ValueError("DSM 返回了异常的认证接口路径")
        self.auth_path = path
        version = min(6, int(auth["maxVersion"]))
        if version < int(auth["minVersion"]):
            raise ValueError("DSM 不支持探路脚本使用的认证版本")
        self.auth_version = str(version)
        payload = self.request(self.auth_path, {
            "api": "SYNO.API.Auth", "version": self.auth_version, "method": "login",
            "account": username, "passwd": password, "session": "DSM", "format": "sid",
        })
        sid = payload.get("data", {}).get("sid")
        if not isinstance(sid, str) or not sid:
            raise ValueError("DSM 登录成功但未返回 sid")
        self.sid = sid
        self.remember(sid)
        self.client.cookies.clear()
        return f"身份验证成功（SYNO.API.Auth v{version}）"

    def logout(self) -> str:
        try:
            if self.sid:
                self.request(self.auth_path, {
                    "api": "SYNO.API.Auth", "version": self.auth_version, "method": "logout",
                    "session": "DSM", "_sid": self.sid,
                })
            return "DSM 会话已注销"
        finally:
            self.sid = None
            self.client.close()


@dataclass
class Result:
    account: str
    step: str
    status: str
    seconds: float
    detail: str


class Recorder:
    def __init__(self, secrets: list[str]) -> None:
        self.secrets = secrets
        self.results: list[Result] = []

    def redact(self, value: str) -> str:
        variants = {v for secret in self.secrets if secret
                    for v in (secret, quote(secret, safe=""), quote_plus(secret))}
        for secret in sorted(variants, key=len, reverse=True):
            value = value.replace(secret, "[凭据已隐藏]")
        # 保留错误文字，转义控制字符，避免错误消息破坏终端显示。
        return "".join(c if c.isprintable() else repr(c)[1:-1] for c in value)

    def add(self, account: str, step: str, status: str, seconds: float, detail: str) -> None:
        row = Result(account, step, status, seconds, self.redact(detail))
        self.results.append(row)
        print(f"[{account}] {step}：{status}（{seconds:.3f}s） {row.detail}", flush=True)

    def run(self, account: str, step: str, action: Callable[[], T]) -> tuple[bool, T | None]:
        start = time.perf_counter()
        try:
            value = action()
        except Exception as error:
            self.add(account, step, "失败", time.perf_counter() - start, describe_error(error))
            return False, None
        self.add(account, step, "成功", time.perf_counter() - start,
                 value if isinstance(value, str) else "操作完成")
        return True, value

    def skip(self, account: str, step: str, reason: str) -> None:
        self.add(account, step, "跳过", 0, reason)

    def summary(self) -> None:
        print("\n汇总表（成功不等于权限符合预期，请与 DSM ACL 逐项核对）")
        print("| 账号 | 项目 | 结果 | 秒 | 详情 |")
        print("|---|---|---|---:|---|")
        for row in self.results:
            detail = row.detail.replace("|", "\\|")
            print(f"| {row.account} | {row.step} | {row.status} | {row.seconds:.3f} | {detail} |")


def describe_error(error: Exception) -> str:
    codes = []
    for name in ("ntstatus", "status", "errno", "winerror"):
        value = getattr(error, name, None)
        if isinstance(value, int):
            codes.append(f"{name}={value} (0x{value & 0xFFFFFFFF:08X})")
    return f"{type(error).__module__}.{type(error).__name__}; {'; '.join(codes) or '无数值错误码'}; {error}"


def is_denied(error: Exception) -> bool:
    # 文件不存在、网络断开、共享冲突等不能当成“权限生效”。
    return (getattr(error, "ntstatus", None) in (0xC0000022, 0xC0000061)
            or getattr(error, "status", None) in (0xC0000022, 0xC0000061)
            or getattr(error, "errno", None) == errno.EACCES)


def relative_path(value: str) -> str:
    path = value.replace("/", "\\")
    parts = path.split("\\")
    if (not value or path.startswith("\\") or any(
        not p or p in (".", "..") or p.endswith((" ", "."))
        or any(c in p for c in ':*?"<>|') or any(ord(c) < 32 for c in p)
        for p in parts
    )):
        raise argparse.ArgumentTypeError("目录必须是共享文件夹内的相对路径，不允许绝对路径或 ..")
    return path


class Workspace:
    """仅记录、删除本次创建的对象；不递归遍历删除用户目录。"""
    def __init__(self, fs: NasFs) -> None:
        self.fs = fs
        self.owned: list[tuple[str, bool]] = []

    def mkdir(self, path: str) -> str:
        self.fs.mkdir(path)
        self.owned.append((path, True))
        return f"已创建 {path}"

    def write(self, path: str, chunks: int = 20) -> str:
        # 父目录是本次新建的 UUID 工作目录，失败时也可以安全清理残留。
        self.owned.append((path, False))
        with self.fs.open(path, "xb"):
            pass
        digest = hashlib.sha256()
        for _ in range(chunks):
            block = uuid.uuid4().bytes * (CHUNK_SIZE // 16)
            # 每次重新以追加模式打开，覆盖断点续传使用的追加语义。
            with self.fs.open(path, "ab") as stream:
                written = stream.write(block)
                if written != len(block):
                    raise OSError("追加写入长度不足")
            digest.update(block)
        return digest.hexdigest()

    def verify(self, path: str, expected: str, size: int = TOTAL_SIZE) -> str:
        digest = hashlib.sha256()
        total = 0
        with self.fs.open(path, "rb") as stream:
            while block := stream.read(CHUNK_SIZE):
                total += len(block)
                digest.update(block)
        if total != size or digest.hexdigest() != expected:
            raise ValueError(f"读回不一致：字节数={total}，SHA-256={digest.hexdigest()}，预期={expected}")
        return f"{total} 字节，SHA-256={expected}"

    def rename(self, source: str, target: str) -> str:
        start = time.perf_counter()
        self.fs.rename(source, target)
        elapsed = time.perf_counter() - start
        self.owned = [(target + p[len(source):] if p == source or p.startswith(source + "\\") else p, d)
                      for p, d in self.owned]
        if elapsed >= 1:
            raise ValueError(f"rename 已完成，但耗时 {elapsed:.6f}s，未达到 <1s 验收标准")
        return f"纯 SMB rename 耗时 {elapsed:.6f}s（未复制文件）"

    def cleanup(self, recorder: Recorder, account: str) -> None:
        for path, directory in reversed(self.owned):
            def remove(p: str = path, d: bool = directory) -> str:
                try:
                    self.fs.delete(p, d)
                except FileNotFoundError:
                    return f"已不存在：{p}"
                except OSError as error:
                    if getattr(error, "ntstatus", None) not in (0xC0000034, 0xC000003A):
                        raise
                return f"已删除：{p}"
            recorder.run(account, "删除/清理", remove)
        self.owned.clear()


def file_checks(fs: NasFs, base: str, recorder: Recorder, account: str) -> None:
    workspace = Workspace(fs)
    root = ntpath.join(base, f"wp00-{account}-{uuid.uuid4().hex}")
    try:
        recorder.run(account, "列目录", lambda: f"目录项数量：{len(fs.list(base))}")
        created, _ = recorder.run(account, "新建文件夹", lambda: workspace.mkdir(root))
        if not created:
            for step in ("追加写入 20MiB", "读回 SHA-256", "同卷 rename", "中文路径", "带空格路径", "200 字符长路径"):
                recorder.skip(account, step, "测试文件夹未创建")
            return
        large = ntpath.join(root, "payload.bin")
        written, digest = recorder.run(account, "追加写入 20MiB", lambda: workspace.write(large))
        verified = False
        if written and digest:
            verified, _ = recorder.run(account, "读回 SHA-256", lambda: workspace.verify(large, digest))
        else:
            recorder.skip(account, "读回 SHA-256", "文件未完整写入")
        if verified:
            renamed = root + "-renamed"
            recorder.run(account, "同卷 rename", lambda: workspace.rename(root, renamed))
            # 即使超时，rename 本身可能已经成功，以清理清单作为实际位置。
            root = workspace.owned[0][0]
            recorder.run(account, "rename 后校验", lambda: workspace.verify(ntpath.join(root, "payload.bin"), digest))
        else:
            recorder.skip(account, "同卷 rename", "20MiB 文件尚未校验通过")
        for name, folder in (("中文路径", "中文目录"), ("带空格路径", "05 详情页"),
                             ("200 字符长路径", "p" * 200)):
            def check(folder_name: str = folder) -> str:
                directory = ntpath.join(root, folder_name)
                workspace.mkdir(directory)
                path = ntpath.join(directory, "验证文件.txt")
                digest = workspace.write(path, chunks=1)
                workspace.verify(path, digest, CHUNK_SIZE)
                return f"读写及 SHA-256 正常；共享内相对路径 {len(path)} 字符，末级目录 {len(folder_name)} 字符"
            recorder.run(account, name, check)
    finally:
        workspace.cleanup(recorder, account)


def permission_check(fs: NasFs, directory: str, deny: bool) -> str:
    # 首先确认父目录存在且可列，防止把路径错误误判为权限拒绝。
    fs.list(directory)
    path = ntpath.join(directory, f"wp00-permission-{uuid.uuid4().hex}.tmp")
    created = False
    try:
        try:
            with fs.open(path, "xb") as stream:
                created = True
                stream.write(b"WP00 permission probe\n")
        except Exception as error:
            if deny and not created and is_denied(error):
                return "符合预期：写入被权限拒绝；" + describe_error(error)
            raise
        if deny:
            raise ValueError("预期只读但实际允许创建文件，请检查 NAS ACL")
        return "同一目录对账号 B 允许写入，权限差异符合预期"
    finally:
        if created:
            # 仍用本人会话清理；不使用另一个账号补删。
            fs.delete(path)


def check_coexistence(items: list[SmbNasFs], base: str) -> str:
    if len(items) != 2 or any(fs.session is None for fs in items):
        raise ValueError("需要两个成功登录的 SMB 会话")
    first, second = items
    if first.cache is second.cache or first.session.connection is second.session.connection:
        raise ValueError("两个账号复用了同一条连接")
    sessions = [fs.session for fs in items]
    for fs in (first, second, first):
        fs.list(base)
    if any(fs.session is not old for fs, old in zip(items, sessions)):
        raise ValueError("复查期间会话被替换")
    return "不同缓存、不同 TCP 连接；A → B → A 列目录成功，原会话未替换"


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse 默认会回显未识别参数，可能包含输错位置的密码。
        self.exit(2, "参数错误：请用 --help 检查格式、相对路径与账号/密码数量。\n")


def parser() -> argparse.ArgumentParser:
    result = SafeParser(description="WP00：DSM 与按账号隔离的 SMB 探路（只操作专用测试目录）", add_help=False)
    result.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    result.add_argument("--host", required=True, help="NAS IPv4 地址或主机名，不含协议/路径/端口")
    result.add_argument("--dsm-port", type=int, help="默认 HTTPS 5001 / HTTP 5000")
    result.add_argument("--https", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--share", type=relative_path, required=True, help="一个共享文件夹名称")
    result.add_argument("--user", action="append", required=True, help="按 A、B 顺序重复 1～2 次")
    result.add_argument("--password", action="append", help="与账号按出现顺序配对；全部省略则隐藏输入")
    result.add_argument("--test-dir", type=relative_path, required=True, help="预先建立的读写测试目录，相对于共享文件夹")
    result.add_argument("--read-only-dir", type=relative_path, required=True, help="A 可读不可写、B 可读写的预建测试目录")
    tls = result.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="信任的 CA PEM 文件")
    tls.add_argument("--insecure", action="store_true", help="仅内网自签名证书探路时跳过 TLS 校验")
    result.add_argument("--timeout", type=float, default=30, help="DSM 请求及 SMB 建连超时秒数，默认 30")
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.host)
        or "\\" in args.share
        or not 1 <= len(args.user) <= 2 or len(set(u.casefold() for u in args.user)) != len(args.user)
        or any(not u.strip() or any(ord(c) < 32 for c in u) for u in args.user)
        or (args.password is not None and len(args.password) != len(args.user))
        or not 0 < args.timeout < float("inf")
        or (args.dsm_port is not None and not 1 <= args.dsm_port <= 65535)
        or args.test_dir.casefold() == args.read_only_dir.casefold()):
        cli.error("参数非法")
    passwords = args.password
    if passwords is None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                passwords = [getpass.getpass(f"请输入账号 {chr(65+i)} 的 NAS 密码：") for i in range(len(args.user))]
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            print("密码输入已取消或终端无法隐藏输入；请在交互终端运行", file=sys.stderr)
            return 130
    args.password = None
    recorder = Recorder(list(passwords))
    # 禁用第三方日志，协议调试日志可能包含认证材料；仅输出经脱敏的步骤结果。
    logging.disable(logging.CRITICAL)
    connections: list[SmbNasFs] = []
    dsm_clients: list[tuple[str, DsmClient]] = []
    interrupted = False
    print("仅在明确授权的专用测试目录运行；结果不自动写盘，请将脱敏汇总填入 RESULT.md。")
    if not args.https:
        print("当前使用 HTTP：DSM 登录凭据没有 TLS 保护。")
    try:
        for index, username in enumerate(args.user):
            account = chr(65 + index)
            dsm = DsmClient(args.host, args.dsm_port or (5001 if args.https else 5000), args.https,
                            args.ca_file or not args.insecure, args.timeout, recorder.secrets.append)
            dsm_clients.append((account, dsm))
            ok, _ = recorder.run(account, "DSM 登录", lambda: dsm.login(username, passwords[index]))
            fs = SmbNasFs(args.host, args.share, username, args.timeout)
            connections.append(fs)
            if not ok:
                recorder.skip(account, "SMB 及文件测试", "DSM 身份验证失败，不尝试 SMB")
                continue
            ok, _ = recorder.run(account, "SMB 连接", lambda: fs.connect(passwords[index]))
            passwords[index] = ""
            if not ok:
                recorder.skip(account, "文件测试", "SMB 建连失败")
                continue
            file_checks(fs, args.test_dir, recorder, account)
            recorder.run(account, "只读目录拒绝写入" if index == 0 else "同目录写权限对照",
                         lambda: permission_check(fs, args.read_only_dir, deny=index == 0))
        if len(connections) == 2 and all(fs.session for fs in connections):
            recorder.run("A+B", "双账号同时存活", lambda: check_coexistence(connections, args.test_dir))
            # 第二账号加入后再检查 A 的拒写，发现全局凭据串用。
            recorder.run("A", "B 加入后仍拒写", lambda: permission_check(connections[0], args.read_only_dir, True))
        else:
            recorder.skip("A+B", "双账号同时存活", "未提供两个账号，或至少一个连接未成功")
    except KeyboardInterrupt:
        interrupted = True
        recorder.add("全部", "中断", "失败", 0, "用户中断；请核对清理记录与残留目录")
    except Exception as error:
        recorder.add("全部", "运行异常", "失败", 0, describe_error(error))
    finally:
        passwords.clear()
        for index, fs in enumerate(connections):
            recorder.run(chr(65 + index), "关闭 SMB", fs.close)
        for account, dsm in dsm_clients:
            recorder.run(account, "DSM 注销", dsm.logout)
        recorder.summary()
        recorder.secrets.clear()
    return 130 if interrupted else int(any(row.status != "成功" for row in recorder.results))


if __name__ == "__main__":
    raise SystemExit(main())
