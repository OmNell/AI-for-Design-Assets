from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from pathlib import Path
import threading
import json
import httpx
from collections.abc import Iterator

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from app.config import DevUser, Settings
from app.db.models import AiAlert, AiCall, Batch, BatchFile, BatchProduct, CompanyIndustryMap, Job, User, utcnow
from app.main import create_app


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    settings.paths.dev_nas_dir = tmp_path / 'nas'
    settings.bootstrap_admin = 'admin'
    settings.dev_users = [DevUser(username=name, password='fake', permissions={'temp': 'write'}) for name in ['one', 'two', 'admin']]
    with TestClient(create_app(settings, log_dir=tmp_path / 'logs')) as client:
        login(client)
        yield client


def login(client: TestClient, user: str = 'one') -> None:
    assert client.post('/api/auth/login', json={'username': user, 'password': 'fake'}).status_code == 200


def uploaded(client: TestClient, paths: list[str], company: str = '企业', project_code: str = 'P001') -> dict:
    source = BytesIO()
    with Image.new('RGB', (1200, 800), 'blue') as image:
        image.save(source, 'JPEG')
    content = source.getvalue()
    response = client.post('/api/batches', json={'folder_name': f'{project_code}_{company}_项目', 'won': True, 'acks': ['P08'],
        'files': [{'rel_path': path, 'size': len(content), 'mtime': 1} for path in paths]})
    assert response.status_code == 200, response.text
    batch = response.json()
    for file in batch['files']:
        base = f"/api/batches/{batch['id']}/files/{file['id']}"
        headers = {'X-Upload-Token': batch['upload_token']}
        assert client.put(base + '/chunks/0', content=content, headers=headers).status_code == 200
        assert client.post(base + '/complete', headers=headers).status_code == 200
    return batch


def save_description(client: TestClient, base: str, product: dict, description: str):
    return client.put(f"{base}/products/{product['id']}/pre-description",
                      json={"description": description, "version": product["version"], "fingerprint": product["fingerprint"]})


def describe_all(client: TestClient, base: str) -> None:
    response = client.get(base + "/pre-descriptions")
    assert response.status_code == 200, response.text
    for product in response.json()["products"]:
        text = "这是一套商务茶具。" if product["kind"] == "set" else "这是一只水杯。"
        saved = save_description(client, base, product, text)
        assert saved.status_code == 200, saved.text


def confirm(client: TestClient, batch: dict) -> dict:
    describe_all(client, f"/api/batches/{batch['id']}")
    response = client.post(f"/api/batches/{batch['id']}/confirm")
    assert response.status_code == 200, response.text
    assert client.app.state.jobs.wait_idle(8)
    return client.get(f"/api/batches/{batch['id']}").json()


def test_six_items_two_sets_order_flags_terms_and_known_company(client: TestClient) -> None:
    with client.app.state.database.session() as session:
        session.add(CompanyIndustryMap(company_name='标准企业', aliases=['别名企业'], company_seq=7, industry_name='酒业', industry_seq=1))
    paths = [f'单品/{name}.jpg' for name in ['正常甲', '正常乙', '正常丙', '无法判断', '一图多品', '调用失败']] + ['套装/正常套装.jpg', '套装/空关系.jpg']
    result = confirm(client, uploaded(client, paths, '别名企业'))
    assert result['status'] == '待核对'
    assert result['recommended_path'] == '01已中标项目/01酒业/07标准企业'
    assert result['ai_progress'] == {'items_total': 6, 'items_done': 5, 'sets_total': 2, 'sets_done': 2, 'failed': 1}
    products = {product['display_name']: product for product in result['products']}
    for product in products.values():
        if product['ai_status'] != 'done':
            continue
        assert len(product['tags']) == 11 and product['free_text']
        assert product["visual_ai_succeeded"]
        for key, tag in product["tags"].items():
            assert tag["source"] == "ai"
            if tag["flag"] == "uncertain":
                assert tag["value"] == "" and tag["terms"] == [] and tag["ai_value"] == "无法判断"
            elif key == "festival":
                assert tag["value"] == tag["ai_value"] == "" and tag["terms"] == []
            else:
                assert tag["value"] and tag["value"] == tag["ai_value"]
    assert any(tag['flag'] == 'uncertain' for tag in products['无法判断']['tags'].values())
    assert products['一图多品']['flags'] == ['multi_object']
    assert products['空关系']['flags'] == ['empty_relation'] and not products['空关系']['relation_confirmed']
    item_ids = {product['id'] for product in products.values() if product['kind'] == 'item'}
    assert {relation['batch_product_id'] for relation in products['正常套装']['relation_items']} <= item_ids
    assert products['正常套装']['relation_items']
    with client.app.state.database.session() as session:
        calls = list(session.scalars(select(AiCall).where(AiCall.batch_id == result['id']).order_by(AiCall.id)))
        last_item = max(index for index, call in enumerate(calls) if call.kind == 'item')
        first_set = min(index for index, call in enumerate(calls) if call.kind == 'set')
        assert last_item < first_set
        assert len([call for call in calls if call.kind == 'set']) == 2
        assert all(call.kind == 'set' for call in calls[last_item + 1:])
        assert len([call for call in calls if not call.success]) == 4


def test_all_failed_retry_owner_admin_and_unknown_location(client: TestClient) -> None:
    batch = uploaded(client, ['单品/调用失败.jpg'])
    result = confirm(client, batch)
    assert result['status'] == 'AI 处理失败' and result['recommended_path'] is None
    login(client, 'two')
    assert client.post(f"/api/batches/{batch['id']}/retry-ai").status_code == 403
    assert client.get(f"/api/batches/{batch['id']}").status_code == 403
    login(client, 'admin')
    assert client.get(f"/api/batches/{batch['id']}").status_code == 200
    assert client.post(f"/api/batches/{batch['id']}/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    assert client.get(f"/api/batches/{batch['id']}").json()['status'] == 'AI 处理失败'
    login(client)
    assert client.post(f"/api/batches/{batch['id']}/retry-ai").status_code == 200
    assert client.app.state.jobs.wait_idle()
    assert client.post('/api/batches/999/retry-ai').status_code == 404
    staged = uploaded(client, ['单品/未确认.jpg'], '陌生企业', project_code='P002')
    assert client.post(f"/api/batches/{staged['id']}/retry-ai").status_code == 409


def test_cancel_pending_and_running_ai_does_not_publish_late_result(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.client import FakeAiClient
    original = FakeAiClient.generate
    entered, release = threading.Event(), threading.Event()
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeAiClient, 'generate', delayed)
    batch = uploaded(client, [f'单品/{index}.jpg' for index in range(6)])
    describe_all(client, f"/api/batches/{batch['id']}")
    assert client.post(f"/api/batches/{batch['id']}/confirm").status_code == 200
    try:
        assert entered.wait(2)
        response = client.post(f"/api/batches/{batch['id']}/cancel", json={'reason': '本次无需继续'})
        assert response.status_code == 200 and response.json()['status'] == '已终止'
    finally:
        release.set()
    assert client.app.state.jobs.wait_idle()
    with client.app.state.database.session() as session:
        assert session.get(Batch, batch['id']).status == '已终止'
        assert all(not product.tags for product in session.scalars(select(BatchProduct).where(BatchProduct.batch_id == batch['id'])))
        assert any(job.status == 'failed' and job.error == '批次已终止' for job in session.scalars(select(Job).where(Job.kind == 'ai_tag_product')))


def test_updated_hint_for_next_batch_and_stall_alert_is_deduplicated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.client import FakeAiClient
    original = FakeAiClient.generate
    seen = []
    def capture(self, image, fields, **kwargs):
        seen.extend(fields)
        return original(self, image, fields, **kwargs)
    monkeypatch.setattr(FakeAiClient, 'generate', capture)
    login(client, 'admin')
    fields = client.get('/api/admin/tag-fields').json()
    fields["fields"] = [{key: field[key] for key in ("key", "hint", "sort_order")} for field in fields["fields"]]
    fields["fields"][0]["hint"] = "仅描述图中可见细节"
    assert client.put('/api/admin/tag-fields', json=fields).status_code == 200
    test = client.post('/api/settings/ai/test')
    assert test.status_code == 200 and len(test.json()['results']) == 3
    assert client.get('/api/admin/ai-alerts').status_code == 200
    login(client)
    assert client.post('/api/settings/ai/test').status_code == 403
    assert client.get('/api/admin/ai-alerts').status_code == 403
    result = confirm(client, uploaded(client, ['单品/正常.jpg']))
    assert len(result['products'][0]['tags']) == 11
    assert result['tag_labels']['category'] == '品类'
    assert any(field['key'] == 'category' and field['hint'] == '仅描述图中可见细节' for field in seen)
    client.app.state.ai.shutdown()
    with client.app.state.database.session() as session:
        batch = session.get(Batch, result['id'])
        batch.status = 'AI 处理中'
        batch.payload = {**batch.payload, 'ai_last_progress': (utcnow() - timedelta(hours=2)).isoformat()}
    client.app.state.ai.check_stalls()
    client.app.state.ai.check_stalls()
    with client.app.state.database.session() as session:
        assert len(list(session.scalars(select(AiAlert)))) == 1
    def stalled() -> list[dict]:
        return [item for item in client.get("/api/todos").json() if item["key"] == "ai_stalled"]

    batch_id = result["id"]
    reminder = stalled()
    assert len(reminder) == 1 and reminder[0]["count"] == 1
    assert reminder[0]["link"] == f"/batches/{batch_id}"
    assert [row["id"] for row in reminder[0]["batches"]] == [batch_id]
    login(client, "two")
    assert stalled() == []
    login(client, "admin")
    assert stalled() == reminder
    assert len(client.get("/api/admin/ai-alerts").json()) == 1

    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        batch.payload = {**batch.payload, "ai_last_progress": utcnow().isoformat()}
    assert stalled() == []
    with client.app.state.database.session() as session:
        batch = session.get(Batch, batch_id)
        batch.payload = {**batch.payload, "ai_run": "下一轮", "ai_last_progress": (utcnow() - timedelta(hours=2)).isoformat()}
    assert stalled() == []
    client.app.state.ai.check_stalls()
    assert stalled() == reminder
    with client.app.state.database.session() as session:
        session.get(Batch, batch_id).status = "AI 处理失败"
    assert stalled() == []
    assert any(item["key"] == "ai_failed" for item in client.get("/api/todos").json())


def test_production_payload_has_only_review_image_and_sanitized_candidate_text(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.client import AiClient
    from app.config import AiConfig
    payloads = []
    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'tags': {'category': '杯子'}, 'free_text': '测试描述', 'relation_items': [1]})}}]})
    config = AiConfig(model_vision='vision-test', api_key_enc=client.app.state.cipher.encrypt('test-key'))
    real = AiClient(config, client.app.state.database, client.app.state.cipher, transport=httpx.MockTransport(transport))
    monkeypatch.setattr(client.app.state.ai, 'client', lambda: real)
    try:
        batch = uploaded(client, ['单品/P001_私密企业_one_杯子.jpg', '套装/套装.jpg'], '私密企业')
        base = f"/api/batches/{batch['id']}"
        item, bundle = client.get(base + "/pre-descriptions").json()["products"]
        assert save_description(client, base, item, "私密企业 P001 one 的蓝牙音箱。资料示例 /volume1/工作台临时区/秘密.doc").status_code == 200
        assert save_description(client, base, bundle, "这是一套便携音箱与充电盒。").status_code == 200
        assert client.post(base + "/confirm").status_code == 200
        assert client.app.state.jobs.wait_idle()
        result = client.get(base).json()
        assert result['status'] == '待核对' and len(payloads) == 2
        text = json.dumps(payloads, ensure_ascii=False)
        for secret in ['私密企业', 'P001', 'one_杯子', '工作台临时区', '设计资产共享', '秘密.doc', 'volume1']:
            assert secret not in text
        assert '[已省略]' in text
        item_description = json.loads(payloads[0]['messages'][1]['content'][0]['text'])["pre_description"]
        assert "蓝牙音箱" in item_description and " one " not in item_description
        content = payloads[1]['messages'][1]['content']
        assert json.loads(content[0]['text']) == {"pre_description": "这是一套便携音箱与充电盒。"}
        import base64
        image = base64.b64decode(content[1]['image_url']['url'].split(',')[1])
        with Image.open(BytesIO(image)) as decoded:
            assert max(decoded.size) <= 1024 and not decoded.getexif()
    finally:
        real.close()


def test_only_visual_success_on_current_fingerprint_counts(client: TestClient) -> None:
    result = confirm(client, uploaded(client, ['单品/正常.jpg', '单品/调用失败.jpg']))
    good, failed = sorted(result["products"], key=lambda item: not item["visual_ai_succeeded"])
    assert good["visual_ai_succeeded"] and not failed["visual_ai_succeeded"]
    with client.app.state.database.session() as session:
        old = session.get(BatchProduct, failed["id"])
        old.ai_status = "done"
        old.tags = {"category": {"value": "手工标签"}}
        image = session.get(BatchFile, good["batch_file_id"])
        image.fingerprint = "0" * 64
    current = client.get(f"/api/batches/{result['id']}").json()
    assert all(not item["visual_ai_succeeded"] for item in current["products"])
