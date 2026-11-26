#!/usr/bin/env python3
"""使用已锁定前端依赖构建并验证 Windows 候选发布 ZIP。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "RELEASE.json"
REQUIRED = {"backend/app/main.py", "backend/app/static/index.html", "backend/pyproject.toml",
            "backend/alembic.ini", "backend/config.production.example.yaml", "deploy/install.ps1",
            "deploy/upgrade.ps1", "deploy/uninstall.ps1", "scripts/restore_backup.py",
            "scripts/rebuild_catalog.py", "scripts/preflight_check.py", "deploy/README.md"}


def sha(stream) -> str:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(path: str) -> bool:
    if not path or "\\" in path or any(ord(c) < 32 or c in ':*?"<>|' for c in path):
        return False
    return all(part and part not in (".", "..") and not part.endswith((" ", ".")) and
               not re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
               for part in path.split("/"))


def eligible(path: str) -> bool:
    source = PurePosixPath(path)
    if not safe_name(path) or any(part.startswith(".") or part in {"data", "logs", "devdata", "node_modules", "__pycache__"}
                                  for part in source.parts):
        return False
    if path in {"README.md", "backend/pyproject.toml", "backend/alembic.ini",
                "backend/config.example.yaml", "backend/config.production.example.yaml"}:
        return True
    if path.startswith(("backend/app/", "backend/alembic/")) and source.suffix in {".py", ".mako", ".txt"}:
        return True
    if path.startswith("backend/app/static/") and source.suffix in {".html", ".js", ".css", ".svg", ".woff2", ".png", ".ico"}:
        return True
    if path.startswith("docs/") and source.suffix == ".md":
        return True
    if path.startswith("deploy/") and source.suffix in {".ps1", ".md", ".py"}:
        return True
    return path in {f"scripts/{name}" for name in
                    ("restore_backup.py", "rebuild_catalog.py", "preflight_check.py", "build_release.py", "build_release.ps1")}


def verify(package: Path, expected_sha: str | None = None, extract: Path | None = None) -> dict:
    if expected_sha:
        with package.open("rb") as stream:
            if sha(stream) != expected_sha.lower():
                raise ValueError("发布 ZIP SHA-256 与批准值不符")
    with ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len({name.casefold() for name in names}) != len(names) or MANIFEST not in names
                or any(not safe_name(item.filename) or item.is_dir() or stat.S_ISLNK(item.external_attr >> 16)
                       or item.flag_bits & 1 for item in entries)):
            raise ValueError("发布包含不安全、重复或未知归档项")
        info = archive.getinfo(MANIFEST)
        if info.file_size > 2 * 1024 * 1024:
            raise ValueError("发布清单大小不合法")
        manifest = json.loads(archive.read(info))
        listed = manifest["files"]
        if manifest["format"] != 1 or set(names) != {*listed, MANIFEST} or not REQUIRED <= set(listed):
            raise ValueError("发布包文件与清单不一致")
        if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.\d+)?", manifest["version"]):
            raise ValueError("不支持的发布版本")
        if not any(name.startswith("backend/app/static/assets/") and name.endswith(".js") for name in listed):
            raise ValueError("发布包缺少 Vite 构建产物")
        for name, expected in listed.items():
            entry = archive.getinfo(name)
            if not eligible(name) or entry.file_size != expected["size"]:
                raise ValueError("发布包包含超出清单范围的文件")
            with archive.open(name) as stream:
                if sha(stream) != expected["sha256"]:
                    raise ValueError("发布文件校验失败")
        if extract:
            extract.mkdir(parents=False, exist_ok=False)
            for name in names:
                target = extract.joinpath(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
        return manifest


def _run(*command: str, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def build(version: str, dist: Path) -> Path:
    if not re.fullmatch(r"v\d+\.\d+\.\d+-rc\.\d+", version):
        raise ValueError("构建阶段只接受候选版本，例如 v1.1.6-rc.1")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("发布构建需要 Python 3.12")
    _run("git", "diff", "--quiet", "HEAD", "--")
    node = subprocess.check_output(["node", "-p", "process.versions.node"], text=True).strip()
    version_info = tuple(map(int, node.split(".")[:2]))
    if version_info < (22, 12) or version_info[0] in (23, 25):
        raise ValueError("锁定的 Vitest 要求 Node 22.12+、24 LTS 或 26+")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    npm_version = subprocess.check_output([npm, "--version"], text=True).strip()
    if int(npm_version.split(".")[0]) < 10:
        raise ValueError("发布构建需要 npm 10 或以上")
    _run(npm, "ci", cwd=ROOT / "frontend")
    _run(npm, "run", "check", cwd=ROOT / "frontend")
    # Vite 的 outDir 直接指向后端静态资源目录。
    paths = {p for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0") if p and eligible(p)}
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "backend/app/static").rglob("*") if p.is_file())
    if not REQUIRED <= paths:
        raise ValueError("缺少已纳入 Git 的发布文件或 Vite 产物")
    dist.mkdir(parents=True, exist_ok=True)
    package = dist / f"nas-workbench-{version}-windows.zip"
    manifest = {"format": 1, "version": version, "built_at": datetime.now(timezone.utc).isoformat(),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "node": node, "python": sys.version.split()[0], "files": {}}
    with ZipFile(package, "x", compression=ZIP_DEFLATED) as out:
        for name in sorted(paths):
            path = ROOT / name
            if not eligible(name) or any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(ROOT)):
                raise ValueError("发布文件含链接或超出允许范围")
            data = path.read_bytes()
            manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            out.writestr(name, data)
        out.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    verify(package)
    with package.open("rb") as stream:
        value = sha(stream)
    package.with_suffix(".zip.sha256").write_text(f"{value}  {package.name}\n", encoding="ascii")
    print(f"{package.absolute()}\nSHA-256 {value}")
    return package


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1.1.6-rc.1")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--expected-sha")
    parser.add_argument("--extract", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify(args.verify, args.expected_sha, args.extract)
        print(f"{result['version']} · {len(result['files'])} 个文件校验通过")
    else:
        build(args.version, args.out)


if __name__ == "__main__":
    main()
