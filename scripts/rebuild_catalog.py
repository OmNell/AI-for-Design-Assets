#!/usr/bin/env python3
"""只读扫描 NAS 档案，并在新的 SQLite 目录恢复可证明的原始身份。"""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import load_settings
from app.services.nas.factory import create_paths, create_system_fs
from app.services.nas.paths import Region, SYSTEM_PERMISSIONS
from app.services.nas.smb import SmbFs
from app.services.recovery import rebuild, rebuild_auxiliary, scan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="新的 JSON 核验报告路径")
    parser.add_argument("--selection", type=Path, help="人工确认项目的候选路径、清单指纹和外部证据说明")
    parser.add_argument("--target", type=Path, help="不存在的新数据库目录，省略时只生成计划")
    parser.add_argument("--resume-derived", type=Path, help="使用相同的已核验计划继续隔离库的派生数据")
    parser.add_argument("--rebuild-derived", action="store_true", help="生成向量和缩略图，真实模式会调用所配置的百炼")
    parser.add_argument("--prompt-system-password", action="store_true", help="密钥也遗失时，在内存中输入 wb-system 密码")
    args = parser.parse_args()
    if args.resume_derived and (args.target or args.rebuild_derived):
        parser.error("--resume-derived 与新目标选项互斥")
    if args.rebuild_derived and args.target is None:
        parser.error("重建派生数据需要指定新的 --target")
    if args.report.exists() or args.report.is_symlink():
        parser.error("恢复报告文件已存在")
    try:
        config = load_settings(args.config)
        if args.target and (args.target.exists() or args.target.is_symlink() or args.target.resolve() == config.paths.data_dir):
            parser.error("目标必须是不存在的隔离目录，禁止覆盖已配置的数据目录")
        if args.resume_derived and (args.resume_derived.is_symlink() or not (args.resume_derived / "app.db").is_file()
                                    or args.resume_derived.resolve() == config.paths.data_dir):
            parser.error("继续执行的目标必须是与源配置分开的已验证隔离库")
        selections = json.loads(args.selection.read_text(encoding="utf-8")) if args.selection else {}
        if not isinstance(selections, dict) or any(not isinstance(v, dict) for v in selections.values()):
            parser.error("人工确认清单必须按项目编号索引")
        paths = create_paths(config)
        if args.prompt_system_password:
            if config.mode != "prod":
                parser.error("仅生产模式支持临时输入系统账号密码")
            nas = config.nas
            fs = SmbFs(str(nas.verified_address or nas.host), nas.share, nas.system_user, getpass.getpass("系统账号密码："),
                       paths, nas.timeout_s, SYSTEM_PERMISSIONS, strict_share=nas.work_root is not None)
        else:
            fs = create_system_fs(config)
        try:
            root = paths.root(Region.FORMAL)
            report, selected = scan(fs, root, {key.casefold(): val for key, val in selections.items()})
            report["phase"] = "plan"
            if args.target and selected:
                report["rebuilt"] = rebuild(fs, root, args.target, selected)
                report["phase"] = "identity_rebuilt"
                if args.rebuild_derived:
                    report["derived"] = rebuild_auxiliary(config, args.target, fs, root, selected)
                    report["phase"] = "derived_verified"
            if args.resume_derived and selected:
                report["derived"] = rebuild_auxiliary(config, args.resume_derived, fs, root, selected)
                report["phase"] = "derived_verified"
        finally:
            fs.close()
    except Exception as error:
        print(f"恢复流程停止（{type(error).__name__}），请核对配置、源档案和目标目录。", file=sys.stderr)
        return 1
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"可恢复 {len(selected)} 个项目，待确认 {len(report['unresolved'])} 个。详见 {args.report}")
    return 2 if report["unresolved"] or report.get("derived", {}).get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
