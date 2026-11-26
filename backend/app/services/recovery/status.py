"""启动时有界抽查正式档案，完整的归档核验由管理员单独启动。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
from threading import Event, Thread
from time import monotonic

from sqlalchemy import func, select

from app.config import Settings
from app.db.database import Database
from app.db.models import CatalogFile, Product, Project, utcnow
from app.services.archive import PROJECT_FILE
from app.services.archive.publication import RECEIPT
from app.services.crypto import CryptoError
from app.services.nas import NasFs, NasNotFound, NasPermissionDenied, Region, safe_join
from app.services.nas.errors import NasError, NasLoginError
from app.services.nas.factory import create_paths, create_system_fs

FORMAL_STATES = ("active", "voided", "deleted")


@dataclass(frozen=True)
class Sample:
    state: str
    checked_at: datetime | None = None
    receipts: tuple[tuple[int, str, int], ...] = ()


def sample_formal(fs: NasFs, root: str, stopping: Event, limit: int = 36,
                  budget_seconds: float = 6.0) -> Sample:
    """只读取少量路径、档案存在性和短凭据，不读取原始业务文件。"""
    deadline = monotonic() + budget_seconds
    directories = deque([""])
    examined = 0
    uncertain = False
    receipts: list[tuple[int, str, int]] = []
    while directories and not stopping.is_set() and monotonic() < deadline and examined < limit:
        relative = directories.popleft()
        directory = safe_join(root, relative) if relative else root
        for name in fs.list(directory):
            if examined >= limit or stopping.is_set() or monotonic() >= deadline:
                uncertain = True
                break
            if name == "_工作台准备":
                continue
            examined += 1
            path = safe_join(directory, name)
            if not fs.stat(path).is_dir:
                continue
            child = safe_join(relative, name) if relative else name
            manifest = safe_join(path, PROJECT_FILE)
            evidence = safe_join(path, RECEIPT)
            if fs.exists(manifest):
                if fs.exists(evidence):
                    try:
                        with fs.open_read(evidence) as stream:
                            receipt = json.loads(stream.read(8193))
                        identifier, code, version = receipt["project_id"], receipt["project_code"], receipt["asset_version"]
                        if (receipt.get("format") == 1 and receipt.get("root") == child
                                and type(identifier) is int and type(version) is int and isinstance(code, str)):
                            receipts.append((identifier, code, version))
                        else:
                            uncertain = True
                    except (ValueError, KeyError, TypeError):
                        uncertain = True
                else:
                    uncertain = True
            elif len(name.split("_")) == 3 and (child.count("/") >= 3 or child.startswith("_已作废项目/")):
                uncertain = True
            elif child.count("/") < 5:
                directories.append(child)
            else:
                uncertain = True
    fully_examined = not (directories or uncertain or stopping.is_set())
    return Sample("empty" if not receipts and fully_examined else "sampled" if receipts else "unconfirmed",
                  utcnow(), tuple(receipts))


class RecoveryStatus:
    def __init__(self, config: Settings, database: Database) -> None:
        self.config = config
        self.database = database
        self.stopping = Event()
        self.sample = Sample("not_checked")

    def _sample(self) -> None:
        nas = self.config.nas
        temporary = self.config.model_copy(update={"nas": nas.model_copy(update={"timeout_s": min(nas.timeout_s, 3)})})
        fs = None
        try:
            fs = create_system_fs(temporary)
            self.sample = sample_formal(fs, create_paths(temporary).root(Region.FORMAL), self.stopping)
        except (CryptoError, NasPermissionDenied):
            self.sample = Sample("system_access_required", utcnow())
        except NasLoginError as error:
            inaccessible = error.status_code in (401, 403)
            self.sample = Sample("system_access_required" if inaccessible else "nas_unavailable", utcnow())
        except NasNotFound:
            self.sample = Sample("formal_root_missing", utcnow())
        except NasError:
            self.sample = Sample("nas_unavailable", utcnow())
        finally:
            if fs is not None:
                try:
                    fs.close()
                except NasError:
                    pass

    def start(self) -> None:
        if self.config.mode != "prod":
            return
        if not self.config.nas.system_password_enc.get_secret_value():
            self.sample = Sample("system_access_required", utcnow())
            return
        Thread(target=self._sample, name="recovery-startup-probe", daemon=True).start()

    def shutdown(self) -> None:
        self.stopping.set()

    def local(self) -> dict:
        with self.database.session() as session:
            totals = {
                "projects": session.scalar(select(func.count()).select_from(Project).where(Project.status.in_(FORMAL_STATES))),
                "products": session.scalar(select(func.count()).select_from(Product).join(Project)
                                           .where(Project.status.in_(FORMAL_STATES))),
                "files": session.scalar(select(func.count()).select_from(CatalogFile).join(Project)
                                        .where(Project.status.in_(FORMAL_STATES))),
            }
            sample = self.sample
            incomplete = False
            if sample.receipts:
                ids = {identifier for identifier, _, _ in sample.receipts}
                rows = {row.id: (row.project_code, row.asset_version) for row in session.scalars(
                    select(Project).where(Project.id.in_(ids), Project.status.in_(FORMAL_STATES)))}
                incomplete = any(rows.get(identifier) is None or rows[identifier][0] != code
                                 or rows[identifier][1] < version for identifier, code, version in sample.receipts)
        state = ("needs_scan" if incomplete else "local_present" if totals["projects"] else
                 "empty_confirmed" if sample.state == "empty" else "empty_unconfirmed")
        return {"catalog_state": state, "nas_state": sample.state, "nas_checked_at": sample.checked_at, **totals}
