"""设计资产工作台。"""
import json
from pathlib import Path
import re


def _version() -> str:
    manifest = Path(__file__).resolve().parents[2] / "RELEASE.json"
    if manifest.is_file():
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        if isinstance(version, str) and re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.\d+)?", version):
            return version
        raise ValueError("发布清单中的版本号不合法")
    return "0.1.0"


__version__ = _version()
