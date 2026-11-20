from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.config import AiConfig
from app.db.database import Database
from app.db.models import TagField
from app.services.ai.client import AiClient
from app.services.ai.parsing import parse_result
from app.services.crypto import SecretCipher
from app.services.thumbs.images import render_variants

pytestmark = pytest.mark.bailian


@pytest.fixture
def real_client(database: Database, tmp_path: Path) -> Iterator[AiClient]:
    required = ['BAILIAN_TEST_API_KEY', 'BAILIAN_TEST_MODEL_VISION', 'BAILIAN_TEST_MODEL_TEXT', 'BAILIAN_TEST_MODEL_EMBED']
    if any(not os.environ.get(name) for name in required):
        pytest.fail('真实百炼验收缺少必要环境变量；请按 README 配置，不输出凭据')
    cipher = SecretCipher(tmp_path / 'secret')
    config = AiConfig(base_url=os.environ.get('BAILIAN_TEST_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
        api_key_enc=cipher.encrypt(os.environ['BAILIAN_TEST_API_KEY']), model_vision=os.environ['BAILIAN_TEST_MODEL_VISION'],
        model_text=os.environ['BAILIAN_TEST_MODEL_TEXT'], model_embed=os.environ['BAILIAN_TEST_MODEL_EMBED'])
    client = AiClient(config, database, cipher)
    yield client
    client.close()


def test_three_models_minimal_connection(real_client: AiClient) -> None:
    rows = real_client.test_models()
    assert all(row['success'] for row in rows), [{'kind': row['kind'], 'error': row['error']} for row in rows if not row['success']]


def test_ten_main_images_tagging_for_human_review(real_client: AiClient, database: Database, tmp_path: Path) -> None:
    location = os.environ.get('BAILIAN_TEST_IMAGES')
    if not location:
        pytest.fail('请用 BAILIAN_TEST_IMAGES 指向已获准发送的10张真实主图目录')
    files = sorted(path for path in Path(location).iterdir() if path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png'})
    assert len(files) >= 10, '真实主图不足10张'
    with database.session() as session:
        fields = [{'key': field.key, 'label': field.label, 'hint': field.hint} for field in
                  session.scalars(select(TagField).where(TagField.enabled.is_(True)).order_by(TagField.sort_order))]
    for number, file in enumerate(files[:10], start=1):
        cache = tmp_path / f'review-{number}'
        with file.open('rb') as original:
            render_variants(original, cache)
        # 仅发已去EXIF的1024 JPEG；报告只显示临时序号及标签，不显示源文件名/路径。
        result = parse_result(real_client.generate((cache / '1024.jpg').read_bytes(), fields, pre_description='这是测试用的蓝色图卡'), fields)
        assert set(result['tags']) == {field['key'] for field in fields}
        print(f'图片{number}：节日场景={result["tags"]["festival"]["value"]}；使用场景={result["tags"]["usage_scene"]["value"]}')
