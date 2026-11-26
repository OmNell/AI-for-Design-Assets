#!/usr/bin/env python3
"""只读扫描 NAS 档案，在隔离 SQLite 恢复原始身份，或核对已有卡片柜。"""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import Settings, load_settings
from app.db.database import Database
from app.services.ai.orchestrator import AiService
from app.services.ai.settings import AiSettingsService
from app.services.auth.service import AuthService
from app.services.crypto import SecretCipher
from app.services.jobs import JobQueue
from app.services.nas import NasFs
from app.services.nas.factory import create_paths, create_session_pool, create_system_fs
from app.services.nas.paths import Region, SYSTEM_PERMISSIONS
from app.services.nas.smb import SmbFs
from app.services.recovery import rebuild, rebuild_auxiliary, scan
from app.services.recovery.incremental import apply_incremental, plan_incremental
from app.services.thumbs import ThumbService


def apply_offline(config: Settings, database: Database, fs: NasFs, root: str, plan: dict) -> dict:
    work = config.model_copy(deep=True)
    work.paths.data_dir = database.data_dir
    cipher = SecretCipher(database.data_dir, read_only=True)
    with create_session_pool(work) as pool:
        auth = AuthService(database, pool, work)
        queue = JobQueue(database, pool)
        ai = AiService(auth, queue, AiSettingsService(auth, cipher), cipher, ThumbService(auth, queue))
        try:
            return apply_incremental(ai, fs, root, plan, host="离线恢复")
        finally:
            ai.close_clients()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="新的 JSON 核验报告路径")
    parser.add_argument("--selection", type=Path, help="人工确认项目的候选路径、清单指纹和外部证据说明")
    parser.add_argument("--target", type=Path, help="不存在的新数据库目录，省略时只生成计划")
    parser.add_argument("--resume-derived", type=Path, help="使用相同的已核验计划继续隔离库的派生数据")
    parser.add_argument("--rebuild-derived", action="store_true", help="生成向量和缩略图，真实模式会调用所配置的百炼")
    parser.add_argument("--compare-data", type=Path, help="比较已有 SQLite 数据目录与 NAS 正式档案")
    parser.add_argument("--apply-plan", type=Path, help="在已有目录应用已核验的增量恢复计划")
    parser.add_argument("--service-stopped", action="store_true", help="确认增量恢复的目标工作台服务已经停止")
    parser.add_argument("--prompt-system-password", action="store_true", help="密钥也遗失时，在内存中输入 wb-system 密码")
    args = parser.parse_args()
    if args.resume_derived and (args.target or args.rebuild_derived):
        parser.error("--resume-derived 与新目标选项互斥")
    if args.rebuild_derived and args.target is None:
        parser.error("重建派生数据需要指定新的 --target")
    if args.compare_data and (args.target or args.resume_derived or args.selection):
        parser.error("--compare-data 与隔离重建及人工证据选项互斥")
    if args.apply_plan and (not args.compare_data or not args.service_stopped):
        parser.error("应用增量计划须指定 --compare-data 并先关闭目标服务，再使用 --service-stopped")
    if args.report.exists() or args.report.is_symlink():
        parser.error("恢复报告文件已存在")
    try:
        config = load_settings(args.config)
        if args.compare_data and (args.compare_data.is_symlink() or not (args.compare_data / "app.db").is_file()
                                  or (args.compare_data / "app.db").is_symlink()):
            parser.error("--compare-data 需要含现有 app.db 的本地数据库目录")
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
                       paths, nas.timeout_s, SYSTEM_PERMISSIONS)
        else:
            fs = create_system_fs(config)
        try:
            root = paths.root(Region.FORMAL)
            if args.compare_data:
                database = Database(args.compare_data.resolve())
                try:
                    if args.apply_plan:
                        plan = json.loads(args.apply_plan.read_text(encoding="utf-8"))
                        report = apply_offline(config, database, fs, root, plan)
                        report["phase"] = "incremental_applied"
                    else:
                        report = plan_incremental(database, fs, root)
                        report["phase"] = "incremental_plan"
                finally:
                    database.close()
            else:
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
    print(f"处理阶段：{report['phase']}。详见 {args.report}")
    return 2 if (report["unresolved"] or report["inaccessible"] or report.get("blocked") or
                 report.get("derived", {}).get("blocked") or any(
                     row["kind"] in {"conflict", "local_only"} for row in report.get("items", []))) else 0


if __name__ == "__main__":
    raise SystemExit(main())
