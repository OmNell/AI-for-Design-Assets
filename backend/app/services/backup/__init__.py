"""只向备份目录写入已核验的数据库归档。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from sqlalchemy import select

from app import __version__
from app.db.models import BackupRecord, Job, utcnow
from app.services.auth.service import AuthService
from app.services.jobs import JobContext, JobFailure
from app.services.maintenance.settings import CST, PUBLISHED_STATES, backup_allowed
from app.services.nas import NasFs, Region
from app.services.nas.factory import create_paths
from app.services.oplog import OplogService

from .format import FILES, inventory, measure, read_manifest


class BackupService:
    def __init__(self, auth: AuthService, config_file: Path) -> None:
        self.auth, self.config_file = auth, config_file
        self.directory = auth.config.paths.data_dir / "backup-stage"
        self.paths = create_paths(auth.config)

    def _reserve(self, job: Job) -> BackupRecord:
        with self.auth.database.session() as session:
            record = session.scalar(select(BackupRecord).where(BackupRecord.job_id == job.id))
            if record is not None:
                return record
            clock = datetime.now(CST)
            while True:
                name = f"wb-backup-{clock:%Y%m%d-%H%M}.zip"
                if session.scalar(select(BackupRecord.id).where(BackupRecord.name == name)) is None:
                    break
                clock += timedelta(minutes=1)
            record = BackupRecord(job_id=job.id, name=name)
            session.add(record)
            session.flush()
            return record

    def _package(self, dest: Path) -> tuple[int, str]:
        if not self.config_file.is_file():
            raise JobFailure("缺少 config.yaml，无法生成可恢复备份", retryable=False)
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.directory) as folder:
            snapshot = Path(folder) / "app.db"
            with closing(sqlite3.connect(self.auth.database.data_dir / "app.db", timeout=20)) as connection:
                connection.execute("VACUUM INTO ?", (str(snapshot),))
            if self.auth.config.mode == "prod":
                with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    published = connection.execute(
                        "SELECT 1 FROM projects WHERE ingested_at IS NOT NULL AND status IN (?, ?, ?) LIMIT 1",
                        PUBLISHED_STATES).fetchone()
                if published is None:
                    raise JobFailure("备份快照没有已发布资产，已停止写入 NAS 备份区", retryable=False)
            sources = (snapshot, self.config_file, self.auth.database.data_dir / "secret.key")
            info = {"version": 1, "app_version": __version__, "identities": inventory(snapshot), "files": {}}
            part = dest.with_suffix(".part")
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out, ZipFile(out, mode="w", compression=ZIP_DEFLATED) as archive:
                for entry, source in zip(FILES, sources, strict=True):
                    digest = hashlib.sha256()
                    size = 0
                    with source.open("rb") as reader, archive.open(entry, "w", force_zip64=True) as writer:
                        while block := reader.read(1024 * 1024):
                            writer.write(block)
                            digest.update(block)
                            size += len(block)
                    info["files"][entry] = {"size": size, "sha256": digest.hexdigest()}
                archive.writestr("manifest.json", json.dumps(info, ensure_ascii=False, sort_keys=True))
            with ZipFile(part) as archive:
                if archive.testzip() is not None:
                    raise JobFailure("本地备份归档校验失败")
            os.replace(part, dest)
        with dest.open("rb") as stream:
            return measure(stream)

    def handle(self, context: JobContext) -> None:
        try:
            self._backup(context)
        except sqlite3.DatabaseError:
            raise JobFailure("生成或校验 SQLite 一致性快照失败") from None
        except (BadZipFile, ValueError):
            raise JobFailure("备份归档结构或内容校验失败", retryable=False) from None
        except OSError as exc:
            raise JobFailure(f"本地备份文件读写失败（{type(exc).__name__}）") from None

    def _backup(self, context: JobContext) -> None:
        with self.auth.database.session() as session:
            if not backup_allowed(session, self.auth.config):
                raise JobFailure("资产库未核验、没有已发布资产或处于只读模式，备份已停止", retryable=False)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.directory.chmod(0o700)
        record = self._reserve(context.job)
        local = self.directory / record.name
        if local.is_file():
            with local.open("rb") as stream:
                length, digest = measure(stream)
            if self.auth.config.mode == "prod" or (record.size, record.fingerprint) != (length, digest):
                # 进程可能在落地和登记之间结束。核对归档后沿用已有文件。
                with ZipFile(local) as archive:
                    manifest = read_manifest(archive)
                    if archive.testzip() is not None:
                        raise JobFailure("本地备份归档损坏，已保留待人工核查", retryable=False)
                    if self.auth.config.mode == "prod" and not manifest["identities"].get("projects", {}).get("count"):
                        raise JobFailure("本地暂存备份没有资产记录，已停止上传", retryable=False)
        else:
            length, digest = self._package(local)
        with self.auth.database.session() as session:
            current = session.get(BackupRecord, record.id)
            if current.fingerprint and (current.size, current.fingerprint) != (length, digest):
                raise JobFailure("已登记的本地备份指纹变化，已停止上传", retryable=False)
            current.size, current.fingerprint = length, digest

        fs = self.auth.pool.system()
        target = self.paths.join(Region.BACKUP, record.name)
        pending = self.paths.join(Region.BACKUP, f".备份上传-{context.job.id}-{digest[:16]}.part")
        if not fs.exists(target):
            with local.open("rb") as reader, fs.open_write(pending) as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            with fs.open_read(pending) as stream:
                if measure(stream) != (length, digest):
                    raise JobFailure("NAS 备份暂存校验失败")
            fs.rename(pending, target)
        with fs.open_read(target) as stream:
            if measure(stream) != (length, digest):
                raise JobFailure("NAS 备份与登记内容不一致，已保留本地备份", retryable=False)
        with self.auth.database.session() as session:
            current = session.get(BackupRecord, record.id)
            current.verified_at = utcnow()
            OplogService(session).write(None, "system", "备份验证通过", "backup", str(record.id),
                                        None, {"name": record.name, "size": length}, "成功", "后台任务")
        self._retain(fs)

    def _retain(self, fs: NasFs) -> None:
        with self.auth.database.session() as session:
            older = list(session.scalars(select(BackupRecord).where(
                BackupRecord.verified_at.is_not(None), BackupRecord.local_retained.is_(True))
                .order_by(BackupRecord.id.desc()).offset(7)))
        for record in older:
            target = self.paths.join(Region.BACKUP, record.name)
            if not fs.exists(target):
                continue
            with fs.open_read(target) as stream:
                if measure(stream) != (record.size, record.fingerprint):
                    continue
            (self.directory / record.name).unlink(missing_ok=True)
            with self.auth.database.session() as session:
                session.get(BackupRecord, record.id).local_retained = False
