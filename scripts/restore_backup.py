#!/usr/bin/env python3
"""核验本地或 NAS 备份，并准备只读隔离恢复实例。"""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
import json
from pathlib import Path
import sys
import tempfile
import warnings
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import Settings, load_settings
from app.db.database import Database
from app.services.backup.format import BackupFormatError
from app.services.backup.recovery import BackupRecoveryError, list_candidates, restore as restore, stage_backup, stage_from_nas
from app.services.nas import NasFs, Region
from app.services.nas.factory import create_paths, create_system_fs
from app.services.nas.paths import Access, SYSTEM_PERMISSIONS
from app.services.nas.smb import SmbFs


def _source_fs(config: Settings, prompt: bool) -> NasFs:
    read_config = config.model_copy(deep=True)
    read_config.recovery.read_only = True
    if not prompt:
        return create_system_fs(read_config)
    if config.mode != "prod":
        raise ValueError("仅生产模式支持临时输入系统账号密码")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("系统账号密码（仅在当前进程内存使用）：")
    nas = config.nas
    try:
        return SmbFs(str(nas.verified_address or nas.host), nas.share, nas.system_user, password,
                     create_paths(config), nas.timeout_s, {**SYSTEM_PERMISSIONS, Region.BACKUP: Access.READ})
    finally:
        password = ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", nargs="?", type=Path, help="本地备份 ZIP 文件")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--list-nas", action="store_true", help="只读检查 NAS 最近备份的文件和清单")
    source.add_argument("--nas-backup", help="从 NAS 选择明确的备份文件名")
    parser.add_argument("--config", type=Path, help="目标环境配置，也是可选的 NAS 访问配置")
    parser.add_argument("--target", type=Path, help="新的隔离恢复目录，默认使用系统临时位置")
    parser.add_argument("--compare-nas", action="store_true", help="恢复后只读核对 NAS 档案，输出增量恢复计划")
    parser.add_argument("--prompt-system-password", action="store_true", help="临时输入 NAS 系统账号密码")
    parser.add_argument("--service-stopped", action="store_true", help="确认目标工作台服务已停止")
    args = parser.parse_args()
    if sum(bool(item) for item in (args.package, args.list_nas, args.nas_backup)) != 1:
        parser.error("指定本地备份、--list-nas 或 --nas-backup 其中之一")
    needs_nas = args.list_nas or args.nas_backup or args.compare_nas
    if needs_nas and not args.config:
        parser.error("访问 NAS 需要通过 --config 指定当前环境配置")
    if args.prompt_system_password and not needs_nas:
        parser.error("临时密码仅用于明确请求的 NAS 访问")
    if args.list_nas and (args.target or args.compare_nas):
        parser.error("--list-nas 只列出备份")
    if not args.list_nas and not args.service_stopped:
        parser.error("恢复前请确认目标工作台已停止，并使用 --service-stopped")
    try:
        config = load_settings(args.config) if args.config else None
        if args.list_nas:
            with closing(_source_fs(config, args.prompt_system_password)) as fs:
                candidates = list_candidates(fs, create_paths(config).root(Region.BACKUP), verify_manifest=True)
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
            return
        target = args.target or Path(tempfile.mkdtemp(prefix="wb-restore-")) / "workbench"
        if args.nas_backup:
            with closing(_source_fs(config, args.prompt_system_password)) as fs:
                report = stage_from_nas(fs, create_paths(config).root(Region.BACKUP), args.nas_backup, target,
                                        destination_config=config)
        else:
            report = stage_backup(args.package, target, destination_config=config)
    except (BackupFormatError, BackupRecoveryError) as exc:
        parser.exit(1, f"恢复失败，{exc}。\n")
    except BadZipFile:
        parser.exit(1, "恢复失败，备份 ZIP 无法读取。\n")
    except Exception as exc:
        parser.exit(1, f"恢复失败（{type(exc).__name__}），请检查归档、NAS 权限和目标目录。\n")
    print(f"隔离恢复目录：{target.resolve()}")
    print(json.dumps(report, ensure_ascii=False))
    if args.compare_nas:
        from app.services.recovery.incremental import plan_incremental

        try:
            with closing(Database(target / "data")) as database, closing(_source_fs(config, args.prompt_system_password)) as fs:
                plan = plan_incremental(database, fs, create_paths(config).root(Region.FORMAL))
            with (target / "nas-difference.json").open("x", encoding="utf-8") as stream:
                json.dump(plan, stream, ensure_ascii=False, indent=2)
        except Exception as exc:
            parser.exit(2, f"备份已恢复，NAS 差异暂时无法确认（{type(exc).__name__}）。\n")
        print("NAS 差异计划已保存到隔离目录的 nas-difference.json。")
    print("先使用隔离目录的 config.yaml 验证数据。切换前确认原实例和目标实例均已停止写入，再按部署说明启用恢复库。")


if __name__ == "__main__":
    main()
