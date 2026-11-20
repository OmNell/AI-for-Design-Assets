"""配置更新窗口及本机原子替换。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Lock

from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import AppError, error_response
from app.services.crypto import restrict_key_permissions


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".nas-config-", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            restrict_key_permissions(temp)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        if os.name != "nt":
            with os_directory(path.parent) as directory:
                os.fsync(directory)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def os_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


class Journal:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "nas-settings-state.json"
        self.backup = directory / "nas-settings-previous.yaml"

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, item: dict) -> None:
        atomic_write(self.path, json.dumps(item, ensure_ascii=False).encode())


class NasChangeGate:
    """计数覆盖 ASGI 响应尾部，包括下载流。"""
    def __init__(self) -> None:
        self._lock = Lock()
        self._active = 0
        self._pending = False

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending

    def freeze(self) -> None:
        with self._lock:
            if self._pending or self._active != 1:
                raise AppError("nas_requests_in_progress", "配置已待重启或还有其他请求执行中，请稍后重试", 409)
            self._pending = True

    def unfreeze(self) -> None:
        with self._lock:
            self._pending = False

    async def __call__(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(("/api/", "/l/")):
            await app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        during_restart = method == "GET" and path in {
            "/api/admin/nas/status", "/api/admin/nas/config", "/api/auth/me", "/api/health",
        }
        with self._lock:
            blocked = self._pending and not during_restart
            if not blocked:
                self._active += 1
        if blocked:
            await error_response(503, "nas_restart_pending", "NAS 配置已保存，待运维重启后继续")(scope, receive, send)
            return
        try:
            await app(scope, receive, send)
        finally:
            with self._lock:
                self._active -= 1
