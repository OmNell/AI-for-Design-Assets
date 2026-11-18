"""开发模式的 NAS 替身；权限仅来源于该用户的配置表。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Mapping

from .errors import NasAlreadyExists, NasPermissionDenied
from .fs import BaseFs, NasStat
from .paths import Access, NasPaths, Region


class LocalFs(BaseFs):
    def __init__(self, root: Path | str, permissions: Mapping[Region, Access], paths: NasPaths | None = None) -> None:
        super().__init__(paths or NasPaths(), permissions)
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise NasPermissionDenied("本地 NAS 根目录不能是符号链接")
        self.root = self.root.resolve()
        # 只初始化区域目录，不写入任何用户文件；确保区域根预先存在。
        for region in Region:
            self._path(self.paths.root(region)).mkdir(parents=True, exist_ok=True)

    def _path(self, path: str) -> Path:
        current = self.root
        for part in path.split("/"):
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise NasPermissionDenied("不允许通过链接或联接点访问本地 NAS")
        if not current.resolve().is_relative_to(self.root):
            raise NasPermissionDenied("路径超出本地 NAS 根目录")
        return current

    def _list(self, path: str) -> list[str]:
        return [item.name for item in self._path(path).iterdir()]

    def _stat(self, path: str) -> NasStat:
        target = self._path(path)
        result = target.stat()
        return NasStat(result.st_size, target.is_dir(), result.st_mtime)

    def _open_raw(self, path: str, mode: str) -> BinaryIO:
        return self._path(path).open(mode)

    def _mkdir(self, path: str, recursive: bool) -> None:
        self._path(path).mkdir(parents=recursive, exist_ok=False)

    def _rename(self, source: str, target: str) -> None:
        src, dst = self._path(source), self._path(target)
        if dst.exists():
            raise NasAlreadyExists("目标文件或目录已存在")
        src.rename(dst)

    def _check_write_directory(self, path: str) -> None:
        super()._check_write_directory(path)
        if not os.access(self._path(path), os.W_OK | os.X_OK):
            raise NasPermissionDenied("本地正式目录不可写")

    def _replace(self, source: str, target: str) -> None:
        self._path(source).replace(self._path(target))

    def _delete(self, path: str, recursive: bool) -> None:
        target = self._path(path)
        if target.is_dir():
            if recursive:
                # 逐项验证链接，不能用跟随目录链接的递归实现。
                for child in tuple(target.iterdir()):
                    self._delete(path + "/" + child.name, True)
            target.rmdir()
        else:
            target.unlink()
