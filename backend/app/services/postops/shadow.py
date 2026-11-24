"""工作台公开版本和预备版本的 NAS 文件隔离。"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager

from app.db.models import PostopPlan
from app.services import archive
from app.services.archive.publication import RECEIPT
from app.services.ingest import IngestService
from app.services.jobs import JobFailure
from app.services.nas import NasFs, Region, safe_join
from app.services.nas.paths import NasPaths
from app.services.upload.fingerprint import fingerprint

WORKSPACE = "_工作台准备"
VERSIONS = "_工作台版本"
MARKER = "_工作台版本标记"
RECEIPT_FILES = {RECEIPT, RECEIPT + ".tmp"}
RESERVED = {archive.PROJECT_FILE, MARKER, *RECEIPT_FILES, *archive.PRODUCT_DIR.values()}
CopyGuard = Callable[[], AbstractContextManager[None]]


def physical(data: dict, row: dict) -> str:
    if row.get("physical_rel_path"):
        return row["physical_rel_path"]
    history = [h for h in data["history"] if h["file_id"] == row["id"] and not h["expected_missing"]]
    return max(history, key=lambda h: h["id"])["new_rel_path"] if history else row["rel_path"]


def verify_file(fs: NasFs, path: str, row: dict) -> None:
    stat = fs.stat(path)
    if stat.is_dir or stat.size != row["size"] or fingerprint(fs, path, row["size"]) != row["fingerprint"]:
        raise JobFailure("文件身份与审核清单不符，已停止执行", retryable=False)


class ShadowTree:
    def __init__(self, fs: NasFs, paths: NasPaths, plan: PostopPlan, batch_no: str, guard: CopyGuard) -> None:
        self.fs, self.paths, self.plan, self.guard = fs, paths, plan, guard
        self.stage = paths.join(Region.FORMAL, WORKSPACE, batch_no)
        self.target = paths.join(Region.FORMAL, plan.target_path)
        self.previous = paths.join(Region.FORMAL, plan.previous_path)

    def _owned(self, root: str) -> bool:
        marker = safe_join(root, MARKER)
        if not self.fs.exists(marker):
            return False
        with self.fs.open_read(marker) as stream:
            return stream.read(128) == self.plan.progress["nonce"].encode("ascii")

    def _claim_stage(self) -> None:
        fs = self.fs
        with self.guard():
            archive.mkdir(fs, self.paths.join(Region.FORMAL, WORKSPACE), recursive=True)
            archive.mkdir(fs, self.stage)
            if self._owned(self.stage):
                return
            if fs.list(self.stage):
                raise JobFailure("预备目录的归属无法核实", retryable=False)
            marker = safe_join(self.stage, MARKER)
            token = self.plan.progress["nonce"].encode("ascii")
            with fs.open_write(marker) as stream:
                if stream.write(token) != len(token):
                    raise JobFailure("预备目录标记未完整写入")

    def _empty_directories(self) -> set[str]:
        """保留原目录与上传中的空目录。未审核登记的实际文件阻断版本切换。"""
        old = self.plan.baseline
        expected = {physical(old, f) for f in old["files"] if f["status"] != "deleted"}
        expected.update(v["path"][len(self.plan.previous_path)+1:] for v in self.plan.sources.values()
                        if v["region"] == "formal" and v["path"].startswith(self.plan.previous_path + "/"))
        expected.update(v["path"][len(self.plan.previous_path)+1:] for v in self.plan.purge
                        if v["path"].startswith(self.plan.previous_path + "/"))
        dirs = set(self.plan.progress.get("upload_directories", []))
        for path, is_dir in IngestService._files(self.fs, self.previous, RESERVED):
            if is_dir:
                dirs.add(path)
            elif path not in expected:
                raise JobFailure("原目录存在清单外文件，请先核对 NAS 与卡片柜", retryable=False)
        return dirs

    def _copy(self, relative: str, row: dict) -> None:
        fs = self.fs
        dest = safe_join(self.stage, relative)
        source = self.paths.join(Region(row["region"]), row["path"])
        verify_file(fs, source, row)
        if fs.exists(dest):
            try:
                verify_file(fs, dest, row)
                return
            except JobFailure:
                # 未发布预备区的同名半成品可用审核数据覆写。
                pass
        with fs.open_read(source) as reader, ExitStack() as stack:
            with self.guard():
                archive.mkdir(fs, dest.rpartition("/")[0], recursive=True)
                writer = stack.enter_context(fs.open_write(dest))
            digest = hashlib.sha256()
            size = 0
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                with self.guard():
                    if size > row["size"] or writer.write(chunk) != len(chunk):
                        raise JobFailure("文件复制大小与审核清单不符")
                digest.update(chunk)
            if size != row["size"] or digest.hexdigest() != row["fingerprint"]:
                raise JobFailure("文件复制 SHA-256 与审核清单不符", retryable=False)
        verify_file(fs, dest, row)

    def verify(self, root: str) -> None:
        if not self._owned(root):
            raise JobFailure("发布目录无法核实归属，请检查 NAS", retryable=False)
        docs = archive.documents(self.plan.manifest)
        if archive.parse(self.fs, root) != archive._canonical(self.plan.manifest):
            raise JobFailure("入库后档案核验失败", retryable=False)
        ignored = {MARKER, *RECEIPT_FILES} if root == self.target else {MARKER}
        files = {path for path, is_dir in IngestService._files(self.fs, root, ignored) if not is_dir}
        if files != self.plan.sources.keys() | docs.keys():
            raise JobFailure("发布目录的文件清单与审核数据不一致", retryable=False)
        for relative, row in self.plan.sources.items():
            verify_file(self.fs, safe_join(root, relative), row)

    def prepare(self) -> None:
        if self.fs.exists(self.target):
            self.verify(self.target)
            return
        self._claim_stage()
        directories = self._empty_directories()
        with self.guard():
            for name in sorted(directories | {*archive.PRODUCT_DIR.values(), archive.HISTORY_DIR}):
                archive.mkdir(self.fs, safe_join(self.stage, name), recursive=True)
        for relative, row in self.plan.sources.items():
            self._copy(relative, row)
        with self.guard():
            archive.write(self.fs, self.stage, archive.documents(self.plan.manifest))
        self.verify(self.stage)

    def install(self) -> None:
        if not self.fs.exists(self.target):
            with self.guard():
                archive.mkdir(self.fs, self.target.rpartition("/")[0], recursive=True)
                self.fs.rename(self.stage, self.target)
        self.verify(self.target)
