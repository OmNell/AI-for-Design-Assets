"""只在内网显式执行；不做错误密码重试，避免触发真实账号锁定策略。"""
import os
from pathlib import Path

import pytest

from app.config import NasConfig
from app.services.auth.backends import DsmAuthBackend


@pytest.mark.nas
def test_dsm_two_accounts_verify_and_logout() -> None:
    required = ["NAS_TEST_HOST", "NAS_TEST_USER_A", "NAS_TEST_PASSWORD_A", "NAS_TEST_USER_B", "NAS_TEST_PASSWORD_B"]
    if any(not os.environ.get(key) for key in required):
        pytest.fail("真实 DSM 参数不完整，请按 README 配置 NAS_TEST_* 环境变量", pytrace=False)
    ca = os.environ.get("NAS_TEST_CA_FILE")
    config = NasConfig(host=os.environ["NAS_TEST_HOST"],
                       dsm_port=int(os.environ.get("NAS_TEST_DSM_PORT", "5001")),
                       dsm_https=os.environ.get("NAS_TEST_DSM_HTTPS", "true").lower() == "true",
                       dsm_ca_file=Path(ca) if ca else None)
    backend = DsmAuthBackend(config)
    for suffix in ("A", "B"):
        # 不让 pytest 的断言重写把真实凭据展开到失败报告。
        verified = backend.verify(os.environ[f"NAS_TEST_USER_{suffix}"], os.environ[f"NAS_TEST_PASSWORD_{suffix}"])
        if not verified:
            pytest.fail("DSM 拒绝测试账号身份，请核对账号权限和认证策略", pytrace=False)
