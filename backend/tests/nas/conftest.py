"""文件接口契约同时用于本地替身和显式启用的真实 NAS。"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from app.services.nas import Access, LocalFs, NasFs, NasPaths, Region, SmbFs


@dataclass
class NasCase:
    first: NasFs
    second: NasFs
    paths: NasPaths
    scope: str

    def temp(self, name: str) -> str:
        return self.paths.join(Region.TEMP, self.scope, name)

    def formal(self, name: str) -> str:
        return self.paths.join(Region.FORMAL, self.scope, name)


@pytest.fixture(params=["local", pytest.param("smb", marks=pytest.mark.nas)])
def nas_case(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[NasCase]:
    if request.param == "local":
        paths = NasPaths()
        first: NasFs = LocalFs(tmp_path, {Region.FORMAL: Access.READ, Region.TEMP: Access.WRITE}, paths)
        second: NasFs = LocalFs(tmp_path, {Region.FORMAL: Access.WRITE, Region.TEMP: Access.WRITE}, paths)
    else:
        required = ["NAS_TEST_HOST", "NAS_TEST_SHARE", "NAS_TEST_FORMAL", "NAS_TEST_TEMP", "NAS_TEST_BACKUP",
                    "NAS_TEST_USER_A", "NAS_TEST_PASSWORD_A", "NAS_TEST_USER_B", "NAS_TEST_PASSWORD_B"]
        if any(not os.environ.get(key) for key in required):
            pytest.fail("真实 NAS 参数不完整，请按 README 配置 NAS_TEST_* 环境变量", pytrace=False)
        paths = NasPaths(os.environ["NAS_TEST_FORMAL"], os.environ["NAS_TEST_TEMP"], os.environ["NAS_TEST_BACKUP"])
        # 每实例独立连接；连接失败不打印参数或原始异常。
        first = SmbFs(os.environ["NAS_TEST_HOST"], os.environ["NAS_TEST_SHARE"],
                      os.environ["NAS_TEST_USER_A"], os.environ["NAS_TEST_PASSWORD_A"], paths)
        try:
            second = SmbFs(os.environ["NAS_TEST_HOST"], os.environ["NAS_TEST_SHARE"],
                           os.environ["NAS_TEST_USER_B"], os.environ["NAS_TEST_PASSWORD_B"], paths)
        except Exception:
            first.close()
            raise
    scope = "wp02-" + uuid.uuid4().hex
    case = NasCase(first, second, paths, scope)
    try:
        first.mkdir(paths.join(Region.TEMP, scope))
        second.mkdir(paths.join(Region.FORMAL, scope))
        yield case
    finally:
        # 只删除本测试的 UUID 子目录；本人会话分别清理，绝不删区域根。
        try:
            for fs, region in ((first, Region.TEMP), (second, Region.FORMAL)):
                path = paths.join(region, scope)
                if fs.exists(path):
                    fs.delete(path, recursive=True)
        finally:
            try:
                first.close()
            finally:
                second.close()
