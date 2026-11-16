"""仅流式读取 NAS，不在工作机复制大文件。"""
import hashlib
from typing import Protocol
from app.services.nas import NasFs


class HashState(Protocol):
    def update(self, data: bytes) -> None: ...
    def hexdigest(self) -> str: ...


def fingerprint(fs: NasFs, path: str, size: int, threshold: int, cached: HashState | None = None) -> tuple[str, str]:
    if size <= threshold:
        if cached is not None:
            return cached.hexdigest(), "full"
        digest = hashlib.sha256()
        with fs.open_read(path) as stream:
            while data := stream.read(1024 * 1024):
                digest.update(data)
        return digest.hexdigest(), "full"
    digest = hashlib.sha256()
    with fs.open_read(path) as stream:
        digest.update(stream.read(4 * 1024 * 1024))
    with fs.open_read(path, offset=max(0, size - 4 * 1024 * 1024)) as stream:
        digest.update(stream.read(4 * 1024 * 1024))
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest(), "quick"
