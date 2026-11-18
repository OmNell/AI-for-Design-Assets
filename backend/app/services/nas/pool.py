"""线程安全的登录会话池；实例锁保护操作，定时线程回收空闲连接。"""
from __future__ import annotations

import threading
from typing import Callable

from .errors import NasSessionExpired, NasUnavailable
from .fs import BaseFs


class SessionPool:
    def __init__(self, factory: Callable[[str, str], BaseFs], system_factory: Callable[[], BaseFs],
                 idle_minutes: float = 30, sweep_seconds: float = 30) -> None:
        if idle_minutes <= 0 or sweep_seconds <= 0:
            raise ValueError("会话超时与巡检间隔必须为正数")
        self._factory = factory
        self._system_factory = system_factory
        self._idle_seconds = idle_minutes * 60
        self._sessions: dict[int, BaseFs] = {}
        self._system: BaseFs | None = None
        self._pins: dict[int, int] = {}
        self._pending_close: set[int] = set()
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._sweep, args=(sweep_seconds,), daemon=True, name="nas-session-sweeper")
        self._thread.start()

    def _ensure_open(self) -> None:
        if self._stopped.is_set():
            raise NasSessionExpired("NAS 会话池已关闭")

    def open(self, login_session_id: int, username: str, password: str) -> BaseFs:
        with self._lock:
            self._ensure_open()
            if self._pins.get(login_session_id) and login_session_id in self._sessions:
                self._pending_close.discard(login_session_id)
                self._sessions[login_session_id].touch()
                return self._sessions[login_session_id]
            # 先建立新连接；认证失败不替换旧会话，也不回退到其他身份。
            current = self._factory(username, password)
            old = self._sessions.pop(login_session_id, None)
            if old is not None:
                try:
                    old.close()
                except NasUnavailable:
                    current.close()
                    raise
            self._sessions[login_session_id] = current
            return current

    def get(self, login_session_id: int) -> BaseFs:
        with self._lock:
            self._ensure_open()
            current = self._sessions.get(login_session_id)
            if current is None:
                raise NasSessionExpired("NAS 会话已过期，请重新登录")
            try:
                if not self._pins.get(login_session_id) and current.expire(self._idle_seconds):
                    self._sessions.pop(login_session_id, None)
                    raise NasSessionExpired("NAS 会话已过期，请重新登录")
                current.touch()
            except (NasUnavailable, NasSessionExpired):
                try:
                    self.close(login_session_id)
                except NasUnavailable:
                    pass
                raise NasSessionExpired("NAS 会话已过期，请重新登录") from None
            return current

    def pin(self, login_session_id: int) -> BaseFs:
        with self._lock:
            current = self.get(login_session_id)
            self._pins[login_session_id] = self._pins.get(login_session_id, 0) + 1
            return current

    def unpin(self, login_session_id: int) -> None:
        with self._lock:
            count = self._pins.get(login_session_id, 0)
            if count <= 1:
                self._pins.pop(login_session_id, None)
                if login_session_id in self._pending_close:
                    self._pending_close.discard(login_session_id)
                    self.close(login_session_id)
            else:
                self._pins[login_session_id] = count - 1

    def close(self, login_session_id: int, force: bool = False) -> None:
        with self._lock:
            if self._pins.get(login_session_id) and not force:
                self._pending_close.add(login_session_id)
                return
            self._pins.pop(login_session_id, None)
            self._pending_close.discard(login_session_id)
            current = self._sessions.pop(login_session_id, None)
            if current is not None:
                current.close()

    def system(self) -> BaseFs:
        with self._lock:
            self._ensure_open()
            if self._system is not None:
                try:
                    if not self._system.expire(self._idle_seconds):
                        self._system.touch()
                        return self._system
                except (NasUnavailable, NasSessionExpired):
                    try:
                        self._system.close()
                    except NasUnavailable:
                        pass
            self._system = self._system_factory()
            return self._system

    def expire_idle(self) -> None:
        with self._lock:
            for login_session_id, current in tuple(self._sessions.items()):
                if self._pins.get(login_session_id):
                    continue
                try:
                    expired = current.expire(self._idle_seconds)
                except NasUnavailable:
                    expired = True
                if expired:
                    self._sessions.pop(login_session_id, None)
            if self._system is not None:
                try:
                    expired = self._system.expire(self._idle_seconds)
                except NasUnavailable:
                    expired = True
                if expired:
                    self._system = None

    def _sweep(self, seconds: float) -> None:
        while not self._stopped.wait(seconds):
            self.expire_idle()

    def shutdown(self) -> None:
        self._stopped.set()
        self._thread.join()
        with self._lock:
            sessions = list(self._sessions.values())
            if self._system is not None:
                sessions.append(self._system)
            self._sessions.clear()
            self._pins.clear()
            self._pending_close.clear()
            self._system = None
            failed = False
            for current in sessions:
                try:
                    current.close()
                except NasUnavailable:
                    failed = True
            if failed:
                raise NasUnavailable("部分 NAS 会话清理失败，已全部作废")

    def __enter__(self) -> SessionPool:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()
