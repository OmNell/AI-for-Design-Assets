"""流式计算完整文件 SHA-256，工作台不保存 NAS 文件副本。"""
import hashlib
from typing import Protocol

from app.services.nas import NasFs, NasUnavailable


class HashState(Protocol):
    def update(self, data: bytes) -> None: ...
    def hexdigest(self) -> str: ...


def fingerprint(fs: NasFs, path: str, size: int, cached: HashState | None = None) -> str:
    if cached is not None:
        return cached.hexdigest()
    digest = hashlib.sha256()
    received = 0
    with fs.open_read(path) as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
            received += len(data)
            if received > size:
                break
    if received != size:
        raise NasUnavailable("计算完整指纹时文件大小发生变化，请重新核对上传进度")
    return digest.hexdigest()
