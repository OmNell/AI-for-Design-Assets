"""设计资产工作台。"""
import json
from pathlib import Path
import re

RELEASE_MANIFEST = Path(__file__).resolve().parents[2] / "RELEASE.json"


def _version() -> str:
    if RELEASE_MANIFEST.is_file():
        version = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))["version"]
        if isinstance(version, str) and re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.\d+)?", version):
            return version
        raise ValueError("发布清单中的版本号不合法")
    return "1.1.6"


__version__ = _version()
