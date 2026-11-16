"""共享文件夹内的区域路径；外部调用不能传入本机路径或 UNC 路径。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .errors import NasPermissionDenied


class Region(StrEnum):
    FORMAL = "formal"
    TEMP = "temp"
    BACKUP = "backup"


class Access(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


SYSTEM_PERMISSIONS = {Region.FORMAL: Access.READ, Region.TEMP: Access.NONE, Region.BACKUP: Access.WRITE}


def safe_join(*parts: str) -> str:
    """严格拒绝歧义路径；返回共享文件夹内的正斜线相对路径。"""
    segments: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part or part.startswith(("/", "\\")):
            raise NasPermissionDenied("路径必须为非空相对路径")
        for segment in part.replace("\\", "/").split("/"):
            if (not segment or segment in (".", "..") or segment.endswith((".", " "))
                or any(ord(c) < 32 or c in ':*?"<>|' for c in segment)
                or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", segment)):
                raise NasPermissionDenied("路径包含非法字符或不安全的片段")
            segments.append(segment)
    if not segments:
        raise NasPermissionDenied("路径不能为空")
    return "/".join(segments)


@dataclass(frozen=True)
class NasPaths:
    formal: str = "设计资产"
    temp: str = "工作台临时区"
    backup: str = "_工作台备份"

    def __post_init__(self) -> None:
        for region in Region:
            root = getattr(self, region.value)
            # 仅配置根允许一个前导 /，运行时拼接仍禁止绝对路径。
            object.__setattr__(self, region.value, safe_join(root.removeprefix("/")))
        roots = [self.root(r).casefold() for r in Region]
        if any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for i, a in enumerate(roots) for b in roots[i+1:]):
            raise ValueError("NAS 三个区域根路径不能重叠")

    def root(self, region: Region) -> str:
        return getattr(self, region.value)

    def join(self, region: Region, *parts: str) -> str:
        return safe_join(self.root(region), *parts)

    def region(self, path: str) -> Region:
        path = safe_join(path)
        for region in Region:
            root = self.root(region)
            if path == root or path.startswith(root + "/"):
                return region
        raise NasPermissionDenied("路径不属于已配置的 NAS 区域")

    def check(self, path: str, permissions: Mapping[Region, Access] | None, write: bool = False) -> str:
        path = safe_join(path)
        region = self.region(path)
        if permissions is not None:
            level = permissions.get(region, Access.NONE)
            if level == Access.NONE or (write and level != Access.WRITE):
                raise NasPermissionDenied("NAS 无权限")
        return path
