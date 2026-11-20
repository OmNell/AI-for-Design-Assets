from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import threading
import time

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select

from app.config import AiConfig
from app.db.database import Database
from app.db.models import AiCall
from app.services.ai.client import AiCallError, AiClient, FakeAiClient
from app.services.ai.parsing import parse_result
from app.services.crypto import SecretCipher

FIELDS = [{'key': 'material', 'label': '材质', 'hint': '主要材料'},
          {'key': 'festival', 'label': '节日场景', 'hint': '适用的具体节日；不针对节日就写「通用」'},
          {'key': 'usage_scene', 'label': '使用场景', 'hint': '用途和使用的场合'}]


def review_image(size: tuple[int, int] = (100, 80), exif: bool = False) -> bytes:
    stream = BytesIO()
    with Image.new('RGB', size, 'red') as image:
        metadata = Image.Exif()
        if exif:
            metadata[271] = '原相机'
        image.save(stream, format='JPEG', exif=metadata)
    return stream.getvalue()


def configuration(tmp_path: Path, **kwargs: object) -> tuple[AiConfig, SecretCipher]:
    cipher = SecretCipher(tmp_path / 'key')
    return AiConfig(base_url='https://provider.example/compatible-mode/v1', api_key_enc=SecretStr(cipher.encrypt('sk-test-private')),
                    model_vision='vision-test', model_text='text-test', model_embed='embed-test', **kwargs), cipher


def chat_response() -> httpx.Response:
    return httpx.Response(200, json={'choices': [{'message': {'content': '{"tags":{"material":"木"},"free_text":"描述"}'}}],
                                   'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


def test_real_payload_sends_only_sanitized_image_prompt_and_candidate_text(database: Database, tmp_path: Path) -> None:
    config, cipher = configuration(tmp_path)
    requests: list[dict] = []
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/compatible-mode/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer sk-test-private'
        requests.append(json.loads(request.content))
        return chat_response()
    client = AiClient(config, database, cipher, httpx.MockTransport(handler))
    try:
        raw = client.generate(review_image(), FIELDS, [{'index': 1, 'name': '安全展示名', 'tags': {'material': {'value': '木'}},
                                                       'account': '不允许元数据'}], filename='P001_企业名_NAS账号.jpg')
        assert parse_result(raw, FIELDS)['tags']['material']['value'] == '木'
        serialized = json.dumps(requests, ensure_ascii=False)
        assert 'P001' not in serialized and '企业名' not in serialized and 'NAS账号' not in serialized and '不允许元数据' not in serialized
        uri = requests[0]['messages'][0]['content'][1]['image_url']['url']
        with Image.open(BytesIO(base64.b64decode(uri.split(',')[1]))) as image:
            assert max(image.size) <= 1024 and not image.getexif()
        with database.session() as session:
            saved = session.scalar(select(AiCall))
            assert saved.model == 'vision-test' and saved.kind == 'set'
            assert (saved.prompt_tokens, saved.completion_tokens, saved.total_tokens) == (10, 5, 15)
            assert saved.success and saved.duration_ms >= 0
    finally:
        client.close()


@pytest.mark.parametrize('image', [b'not-an-image', review_image((1025, 1)), review_image(exif=True)])
def test_unsanitized_or_oversized_source_never_reaches_external_model(database: Database, tmp_path: Path, image: bytes) -> None:
    config, cipher = configuration(tmp_path)
    sent: list[httpx.Request] = []
    client = AiClient(config, database, cipher, httpx.MockTransport(lambda req: sent.append(req) or chat_response()))
    try:
        with pytest.raises(AiCallError):
            client.generate(image, FIELDS)
        assert sent == []
    finally:
        client.close()


@pytest.mark.parametrize('status,count', [(401, 1), (429, 4), (503, 4)])
def test_retry_budget_and_provider_error_redaction(database: Database, tmp_path: Path, status: int, count: int) -> None:
    config, cipher = configuration(tmp_path)
    requests: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        content = payload['messages'][0]['content']
        return httpx.Response(status, json={'error': {'message': '厂商错误原文 sk-test-private ' + content[0]['text'] + content[1]['image_url']['url']}})
    client = AiClient(config, database, cipher, httpx.MockTransport(handler), retry_delay_seconds=0)
    try:
        with pytest.raises(AiCallError) as failed:
            client.generate(review_image(), FIELDS)
        assert failed.value.message == f'AI接口返回HTTP {status}'
        assert 'sk-test-private' not in failed.value.message and 'base64' not in failed.value.message
        assert '启用字段' not in failed.value.message
        assert len(requests) == count
        with database.session() as session:
            rows = list(session.scalars(select(AiCall)))
            assert len(rows) == count and all(not row.success and 'sk-test-private' not in row.error for row in rows)
    finally:
        client.close()


def test_timeout_retries_and_logs_each_attempt(database: Database, tmp_path: Path) -> None:
    config, cipher = configuration(tmp_path, retries=1)
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('不能传播的URL与凭据')
    client = AiClient(config, database, cipher, httpx.MockTransport(handler), retry_delay_seconds=0)
    try:
        with pytest.raises(AiCallError, match='AI请求超时'):
            client.generate(review_image(), FIELDS)
        with database.session() as session:
            assert len(list(session.scalars(select(AiCall)))) == 2
    finally:
        client.close()


def test_connection_check_calls_each_model_once_and_returns_individual_results(database: Database, tmp_path: Path) -> None:
    config, cipher = configuration(tmp_path)
    requests: list[dict] = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload['model'] == 'text-test':
            return httpx.Response(403, json={'error': {'code': 'AccessDenied', 'message': '厂商错误原文'}})
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'embedding': [.1, .2]}], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
        return chat_response()
    client = AiClient(config, database, cipher, httpx.MockTransport(handler))
    try:
        rows = client.test_models()
        assert [row['success'] for row in rows] == [True, False, True]
        assert rows[1]['error'] == 'AI接口返回HTTP 403（AccessDenied，账号无调用权限）'
        assert len(requests) == 3 and requests[2]['input'] == '连接测试'
        with database.session() as session:
            assert len(list(session.scalars(select(AiCall)))) == 3
    finally:
        client.close()


def test_connection_diagnostics_omit_untrusted_provider_content(
    database: Database, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    config, cipher = configuration(tmp_path)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={'error': {
            'code': '不可信字段 sk-test-private',
            'message': '原始错误 sk-test-private data:image/jpeg;base64,QUJDREVG',
            'reasoning_content': '私有思考内容',
        }})
    client = AiClient(config, database, cipher, httpx.MockTransport(handler))
    try:
        rows = client.test_models()
        assert all(row['error'] == 'AI接口返回HTTP 403' for row in rows)
        with database.session() as session:
            saved = [row.error for row in session.scalars(select(AiCall))]
        visible = str(rows) + str(saved) + caplog.text
        for secret in ('原始错误', 'sk-test-private', 'QUJDREVG', '私有思考内容'):
            assert secret not in visible
    finally:
        client.close()


def test_concurrency_limit_is_shared_across_client_instances(database: Database, tmp_path: Path) -> None:
    config, cipher = configuration(tmp_path, concurrency=2)
    guard = threading.Lock()
    active, peak = 0, 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with guard:
            active -= 1
        return chat_response()
    clients = [AiClient(config, database, cipher, httpx.MockTransport(handler)) for _ in range(2)]
    try:
        image = review_image()
        with ThreadPoolExecutor(max_workers=6) as workers:
            futures = [workers.submit(clients[index % 2].generate, image, FIELDS) for index in range(6)]
            assert all(future.result() for future in futures)
        assert peak == 2
        with database.session() as session:
            assert len(list(session.scalars(select(AiCall)))) == 6
    finally:
        for client in clients:
            client.close()


def test_fake_is_deterministic_and_triggers_all_requested_cases(database: Database, tmp_path: Path) -> None:
    cipher = SecretCipher(tmp_path / 'key')
    client = FakeAiClient(AiConfig(retries=3), database, cipher)
    image = review_image()
    try:
        assert client.generate(image, FIELDS, filename='普通.jpg') == client.generate(image, FIELDS, filename='普通.jpg')
        uncertain = parse_result(client.generate(image, FIELDS, filename='无法判断.jpg'), FIELDS)
        assert uncertain['tags']['material']['flag'] == 'uncertain'
        multi = parse_result(client.generate(image, FIELDS, filename='一图多品.jpg'), FIELDS)
        assert multi['flags'] == ['multi_object']
        empty = parse_result(client.generate(image, FIELDS, [{'index': 1, 'name': '单品', 'tags': {}}], filename='空关系.jpg'), FIELDS, 'set', {1: 100})
        assert empty['flags'] == ['empty_relation']
        with pytest.raises(AiCallError, match='假模型调用失败'):
            client.generate(image, FIELDS, filename='调用失败.jpg')
        with database.session() as session:
            failed = list(session.scalars(select(AiCall).where(AiCall.success.is_(False))))
            assert len(failed) == 4
    finally:
        client.close()


def sse_response(*events: dict[str, object]) -> httpx.Response:
    content = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
    return httpx.Response(200, text=content + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})


def test_configured_qwen_models_send_valid_requests_and_return_real_results(database: Database, tmp_path: Path) -> None:
    config, cipher = configuration(tmp_path, retries=0)
    config.model_vision = "qwen3.8-omni-flash"
    config.model_text = "qwen4-max"
    config.model_embed = "qwen3.7-text-embedding"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model = payload["model"]
        seen.append(model)
        if model == config.model_vision:
            assert request.url.path.endswith("/chat/completions")
            assert payload["stream"] is True and payload["modalities"] == ["text"]
            assert payload["reasoning_effort"] == "none" and payload["max_tokens"] == 256
            media = payload["messages"][0]["content"][1]["image_url"]["url"]
            prefix, encoded = media.split(",")
            assert prefix == "data:image/jpeg;base64"
            with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as image:
                assert image.format == "JPEG" and image.size == (128, 128) and not image.getexif()
                image.load()
            return sse_response(
                {"choices": [{"delta": {"reasoning_content": "私有思考"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "蓝"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "色"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 13, "completion_tokens": 3, "total_tokens": 16}},
            )
        if model == config.model_text:
            assert request.url.path.endswith("/chat/completions")
            assert payload["enable_thinking"] is False and payload["max_completion_tokens"] == 256
            assert "max_tokens" not in payload
            return httpx.Response(200, json={"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]})
        assert request.url.path.endswith("/embeddings")
        assert payload == {"model": config.model_embed, "input": "连接测试"}
        return httpx.Response(200, json={"model": model, "data": [{"embedding": [0.01, -0.2]}]})

    client = AiClient(config, database, cipher, httpx.MockTransport(handler))
    try:
        rows = client.test_models()
        assert seen == [config.model_vision, config.model_text, config.model_embed]
        assert [row["success"] for row in rows] == [True, True, True]
        with database.session() as session:
            calls = list(session.scalars(select(AiCall).order_by(AiCall.id)))
            assert len(calls) == 3 and all(row.success and row.error is None for row in calls)
            assert (calls[0].prompt_tokens, calls[0].completion_tokens, calls[0].total_tokens) == (13, 3, 16)
    finally:
        client.close()


@pytest.mark.parametrize(("reply", "diagnosis"), [
    ({"choices": [{"message": {"content": "好"}, "finish_reason": "length"}]}, "输出截断"),
    ({"choices": [{"message": {"content": None, "reasoning_content": "私有思考"}, "finish_reason": "stop"}]}, "未返回最终回答"),
    ({"choices": [{"message": {"content": [{"type": "reasoning", "text": "私有思考"}]}, "finish_reason": "stop"}]}, "格式不兼容"),
    ({"choices": [{"message": {"content": "好"}, "finish_reason": None}]}, "格式不兼容"),
    ({"choices": []}, "格式不兼容"),
    ({"error": {"code": "ModelNotFound", "message": "私有思考"}}, "AI模型返回错误（ModelNotFound"),
    ({"code": "InvalidParameter", "message": "私有思考"}, "AI模型返回错误（InvalidParameter"),
    ({"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}, "AI模型返回错误"),
    ({"choices": [{"message": {"refusal": "私有思考"}, "finish_reason": "stop"}]}, "模型拒绝回答"),
])
def test_connection_rejects_unusable_chat_reply(
    database: Database, tmp_path: Path, reply: dict[str, object], diagnosis: str,
) -> None:
    config, cipher = configuration(tmp_path)
    client = AiClient(config, database, cipher, httpx.MockTransport(lambda request: httpx.Response(200, json=reply)))
    try:
        rows = client.test_models()
        assert all(not row["success"] for row in rows)
        assert diagnosis in rows[1]["error"] and "私有思考" not in str(rows)
    finally:
        client.close()


@pytest.mark.parametrize(("frame", "diagnosis"), [
    ({"choices": [{"delta": {"content": "好"}, "finish_reason": "length"}]}, "输出截断"),
    ({"choices": [{"delta": {"content": "好"}, "finish_reason": None}]}, "格式不兼容"),
    ({"error": {"code": "InvalidParameter", "message": "敏感原文"}}, "AI模型返回错误"),
    ({"result": "未知响应"}, "格式不兼容"),
])
def test_omni_stream_rejects_truncation_and_errors(
    database: Database, tmp_path: Path, frame: dict[str, object], diagnosis: str,
) -> None:
    config, cipher = configuration(tmp_path)
    config.model_vision = "qwen3.8-omni-flash"
    client = AiClient(config, database, cipher, httpx.MockTransport(lambda request: sse_response(frame)))
    try:
        first = client.test_models()[0]
        assert not first["success"] and diagnosis in first["error"] and "敏感原文" not in first["error"]
    finally:
        client.close()


@pytest.mark.parametrize(("exception", "diagnosis"), [
    (httpx.ConnectError, "无法建立AI连接"),
    (httpx.ConnectTimeout, "AI连接超时"),
    (httpx.ReadTimeout, "AI请求超时"),
    (httpx.RemoteProtocolError, "AI HTTP 请求失败"),
    (httpx.DecodingError, "AI响应格式不兼容"),
])
def test_connection_transport_diagnostics_are_distinct(
    database: Database, tmp_path: Path, exception: type[httpx.HTTPError], diagnosis: str,
) -> None:
    config, cipher = configuration(tmp_path)
    def fail(request: httpx.Request) -> httpx.Response:
        raise exception("sk-test-private", request=request)

    client = AiClient(config, database, cipher, httpx.MockTransport(fail))
    try:
        rows = client.test_models()
        assert all(not row["success"] and diagnosis in row["error"] and "sk-test-private" not in row["error"] for row in rows)
    finally:
        client.close()


@pytest.mark.parametrize("response_body", [
    b"not json",
    b'{"data":[{"embedding":[0,0]}]}',
    b'{"data":[{"embedding":[1,NaN]}]}',
    b'{"data":[{"embedding":[true,1]}]}',
    b'{"data":[{"embedding":[0.4]}],"model":"different-model"}',
])
def test_embedding_probe_rejects_unusable_vectors(database: Database, tmp_path: Path, response_body: bytes) -> None:
    config, cipher = configuration(tmp_path)
    config.model_embed = "qwen3.7-text-embedding"
    client = AiClient(config, database, cipher, httpx.MockTransport(lambda request: httpx.Response(200, content=response_body)))
    try:
        assert client.test_models()[2]["success"] is False
        with pytest.raises(AiCallError):
            client.embed("查询", None)
    finally:
        client.close()


@pytest.mark.parametrize(("task", "budget"), [("query", 2048), ("rank", 3072)])
def test_search_chat_has_separate_budget_and_only_returns_final_content(database: Database, tmp_path: Path,
                                                                      task: str, budget: int) -> None:
    config, cipher = configuration(tmp_path, retries=0)
    config.model_text = "qwen4-max"
    final = '{"items":[]}' if task == "rank" else '{"fields":{},"keywords":[],"exclude":[]}'

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == config.model_text
        assert payload["enable_thinking"] is False and payload["max_completion_tokens"] == budget
        assert "max_tokens" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"reasoning_content": "不能输出的思考", "content": [
            {"type": "reasoning", "text": "也不能输出的思考"}, {"type": "text", "text": final},
        ]}, "finish_reason": "stop"}]})

    client = AiClient(config, database, cipher, httpx.MockTransport(handler))
    try:
        assert client.search_chat(task, {"query": "测试"}) == final
    finally:
        client.close()
