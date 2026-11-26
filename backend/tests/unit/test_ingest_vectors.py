"""正式向量的输入、生产接口协议与校验。"""
import json

import httpx
import pytest

from app.config import AiConfig
from app.services.ai.client import AiCallError, AiClient, FakeAiClient
from app.services.crypto import SecretCipher
from app.services.ingest.vectors import _inputs


def test_text_uses_confirmed_labels_and_redacts_business_identifiers():
    data = {"products": [{"id": 2, "display_name": "企业杯", "free_text": "P092 内部 可装饮品"}], "tags": [
        {"product_id": 2, "field_key": "category", "value": "无法判断、餐具", "confirmation_status": "confirmed_value"},
        {"product_id": 2, "field_key": "size", "value": "严重错误", "confirmation_status": "pending"},
        {"product_id": 2, "field_key": "material", "value": "", "confirmation_status": "confirmed_unknown"},
    ]}
    assert _inputs(data, ["企业", "P092", "内部"]) == {2: "[已省略]杯\n餐具\n[已省略] [已省略] 可装饮品"}


def test_provider_embedding_and_fake_use_separate_paths(database, tmp_path):
    calls = []
    cipher = SecretCipher(tmp_path)
    config = AiConfig(model_embed="text-embedding-v3", api_key_enc=cipher.encrypt("test-token"), retries=0)

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "text-embedding-v3", "data": [{"embedding": [0.5, -0.25]}]})

    client = AiClient(config, database, cipher, transport=httpx.MockTransport(transport))
    fake = FakeAiClient(config, database, cipher, transport=httpx.MockTransport(transport))
    try:
        assert client.embed("杯", None) == [0.5, -0.25]
        assert calls == [{"model": "text-embedding-v3", "input": "杯"}]
        assert fake.embed("杯", None) == fake.embed("杯", None)
        assert len(calls) == 1
        with pytest.raises(AiCallError):
            fake.embed("向量失败", None)
    finally:
        client.close()
        fake.close()


@pytest.mark.parametrize("payload", [{"embedding": [0, 0]}, {"embedding": [True, 1]}, {"embedding": [1e35]}, {"embedding": []}])
def test_production_rejects_invalid_vectors(database, tmp_path, payload):
    cipher = SecretCipher(tmp_path)
    client = AiClient(AiConfig(model_embed="prod-embed", api_key_enc=cipher.encrypt("test-token"), retries=0), database, cipher,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"model": "prod-embed", "data": [payload]})))
    try:
        with pytest.raises(AiCallError, match="有效向量"):
            client.embed("杯", None)
    finally:
        client.close()
