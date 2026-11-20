"""通过专用只读会话核对目录和目录访问掩码。"""
from __future__ import annotations

from contextlib import contextmanager
import ipaddress
from pathlib import Path
import re
import socket
from time import monotonic
from typing import Iterator

from smbprotocol.exceptions import NoMoreFiles, NoSuchFile
from smbprotocol.file_info import FileAttributes, FileInformationClass
from smbprotocol.open import CreateDisposition, CreateOptions, DirectoryAccessMask, ImpersonationLevel, Open

from app.config import NasConfig, Settings
from app.errors import AppError
from app.services.auth.backends import AuthUnavailable, DsmAuthBackend, FakeAuthBackend
from app.services.nas.errors import NasNotFound, NasPermissionDenied, NasUnavailable, translate
from app.services.nas.paths import REGION_NAMES, SYSTEM_PERMISSIONS, Region, work_path
from app.services.nas.smb import direct_session

RFC1918 = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
MAX_DIRS = 60
READ = DirectoryAccessMask.FILE_LIST_DIRECTORY
ATTRIBUTES = DirectoryAccessMask.FILE_READ_ATTRIBUTES
ADD_FILE = DirectoryAccessMask.FILE_ADD_FILE
ADD_DIR = DirectoryAccessMask.FILE_ADD_SUBDIRECTORY
DELETE_CHILD = DirectoryAccessMask.FILE_DELETE_CHILD
MUTATE = (ADD_FILE, ADD_DIR, DELETE_CHILD, DirectoryAccessMask.FILE_WRITE_ATTRIBUTES, DirectoryAccessMask.DELETE)


def target_name(host: str, share: str) -> None:
    if (not re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", host)
            or host.lower() in {"localhost", "metadata"} or host.lower().endswith((".localhost", ".localdomain"))
            or (re.fullmatch(r"[0-9.]+", host) and not valid_private(host))
            or any(re.fullmatch(r"(?:0x[0-9a-f]+|0[0-7]+)", part, re.I) for part in host.split("."))):
        raise AppError("invalid_nas_target", "NAS 地址必须是公司私网 IPv4 或内网域名", 422)
    try:
        if "/" in work_path(share) or share.endswith("$"):
            raise NasPermissionDenied()
    except NasPermissionDenied:
        raise AppError("invalid_nas_share", "请输入单个普通 SMB 共享文件夹名称", 422) from None


def valid_private(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return any(ip in network for network in RFC1918) and all(ip != net.network_address and ip != net.broadcast_address for net in RFC1918)


def endpoint(host: str) -> ipaddress.IPv4Address:
    try:
        results = {row[4][0] for row in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise AppError("nas_dns_unavailable", "NAS 域名无法解析，请核对内网 DNS", 422) from None
    if len(results) != 1 or not all(valid_private(address) for address in results):
        raise AppError("invalid_nas_address", "NAS 必须只解析到一个 RFC1918 私网 IPv4", 422)
    address = next(iter(results))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError as error:
            from errno import EADDRNOTAVAIL
            if error.errno not in {EADDRNOTAVAIL, 10049}:
                raise AppError("nas_address_check_failed", "无法确认 NAS 不是本机地址", 422) from None
        else:
            raise AppError("invalid_nas_address", "禁止使用工作台本机地址", 422)
    return ipaddress.IPv4Address(address)


class SmbRead:
    def __init__(self, nas: NasConfig, username: str, password: str) -> None:
        self.tree = direct_session(str(nas.verified_address or nas.host), nas.share, username, password, min(3, nas.timeout_s))
        self.identity = str(self.tree.session.connection.server_guid or "")
        self.deadline = monotonic() + 12

    def _limit(self) -> None:
        if monotonic() >= self.deadline:
            raise NasUnavailable("SMB 目录检查超过时限")

    @contextmanager
    def _open(self, path: str, mask: int) -> Iterator[Open]:
        self._limit()
        item = Open(self.tree, path.replace("/", "\\"))
        try:
            item.create(ImpersonationLevel.Impersonation, mask, 0, 7, CreateDisposition.FILE_OPEN,
                        CreateOptions.FILE_DIRECTORY_FILE | CreateOptions.FILE_OPEN_REPARSE_POINT)
            if item.file_attributes & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                raise NasPermissionDenied("NAS 重解析目录不可用")
            if not item.file_attributes & FileAttributes.FILE_ATTRIBUTE_DIRECTORY:
                raise NasNotFound("路径不是目录")
            yield item
        except Exception as error:
            raise translate(error) from None
        finally:
            try:
                item.close()
            except Exception:
                pass

    def _ancestors(self, path: str) -> None:
        parts = path.split("/") if path else []
        for index in range(1, len(parts)):
            with self._open("/".join(parts[:index]), ATTRIBUTES):
                pass

    def check(self, path: str, mask: int) -> None:
        path = work_path(path, allow_share=True)
        self._ancestors(path)
        with self._open(path, mask):
            pass

    def nodes(self, path: str) -> tuple[list[str], bool]:
        path = work_path(path, allow_share=True)
        self._ancestors(path)
        nodes: list[str] = []
        complete = False
        with self._open(path, READ | ATTRIBUTES) as handle:
            # 每次返回有上限，不遍历文件树，也不查询文件正文。
            for _ in range(4):
                self._limit()
                try:
                    entries = handle.query_directory("*", FileInformationClass.FILE_DIRECTORY_INFORMATION, max_output=8192)
                except (NoMoreFiles, NoSuchFile):
                    complete = True
                    break
                if not entries:
                    complete = True
                    break
                for row in entries:
                    flags = row["file_attributes"].get_value()
                    if not flags & FileAttributes.FILE_ATTRIBUTE_DIRECTORY or flags & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    try:
                        name = row["file_name"].get_value().decode("utf-16-le")
                        if "/" in work_path(name):
                            continue
                        child = work_path(f"{path}/{name}" if path else name)
                        with self._open(child, READ | ATTRIBUTES):
                            nodes.append(name)
                    except (NasPermissionDenied, NasNotFound, UnicodeError):
                        continue
                    if len(nodes) >= MAX_DIRS:
                        return sorted(set(nodes), key=str.casefold), True
        return sorted(set(nodes), key=str.casefold), not complete

    def close(self) -> None:
        try:
            self.tree.session.connection.disconnect(timeout=3)
        except Exception:
            pass


class LocalRead:
    def __init__(self, root: Path, permissions: dict, share: str) -> None:
        if root.is_symlink() or not root.is_dir():
            raise NasNotFound("开发 SMB 共享目录不存在")
        self.root = root.resolve()
        self.permissions = permissions
        self.identity = f"LocalFs:{share}"

    def _path(self, path: str, mask: int) -> Path:
        path = work_path(path, allow_share=True)
        current = self.root
        for part in path.split("/") if path else ():
            current /= part
            if current.is_symlink() or current.is_junction():
                raise NasPermissionDenied("目录包含链接或联接点")
            region = next((key for key, value in REGION_NAMES.items() if value == part), None)
            if region and (self.permissions.get(region, "none") == "none" or
                           mask & ~(READ | ATTRIBUTES) and self.permissions.get(region) != "write"):
                raise NasPermissionDenied("模拟 NAS 无权限")
        if not current.resolve().is_relative_to(self.root):
            raise NasPermissionDenied("路径越界")
        if not current.is_dir():
            raise NasNotFound("NAS 文件夹不存在")
        return current

    def check(self, path: str, mask: int) -> None:
        self._path(path, mask)

    def nodes(self, path: str) -> tuple[list[str], bool]:
        result = []
        for index, child in enumerate(self._path(path, READ).iterdir()):
            if index >= 500 or len(result) == MAX_DIRS:
                return sorted(result, key=str.casefold), True
            try:
                if "/" not in work_path(child.name):
                    self.check(f"{path}/{child.name}" if path else child.name, READ)
                    result.append(child.name)
            except (NasPermissionDenied, NasNotFound):
                continue
        return sorted(result, key=str.casefold), False

    def close(self) -> None:
        pass


class Connections:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def dsm(self, nas: NasConfig, username: str | None = None, password: str = "") -> None:
        if self.config.mode == "dev":
            if username and not FakeAuthBackend(self.config.dev_users).verify(username, password):
                raise AuthUnavailable("模拟 DSM 账号或密码不正确")
            return
        backend = DsmAuthBackend(nas.model_copy(update={"timeout_s": min(5, nas.timeout_s)}))
        backend.discover()
        if username and not backend.verify(username, password):
            raise AuthUnavailable("DSM 拒绝该账号或密码")

    @contextmanager
    def open(self, nas: NasConfig, username: str, password: str = "", *, system: bool = False) -> Iterator[LocalRead | SmbRead]:
        if self.config.mode == "prod":
            reader = SmbRead(nas, username, password)
        else:
            if nas.share != self.config.nas.share:
                raise NasNotFound("开发模式只提供当前共享文件夹")
            if system:
                permissions = dict(SYSTEM_PERMISSIONS)
            else:
                if not FakeAuthBackend(self.config.dev_users).verify(username, password):
                    raise NasPermissionDenied("模拟 SMB 账号或密码不正确")
                permissions = next(item.permissions for item in self.config.dev_users if item.username == username)
            reader = LocalRead(self.config.paths.dev_nas_dir, permissions, nas.share)
        try:
            yield reader
        finally:
            reader.close()
