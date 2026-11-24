import json
import sys

import httpx
import pytest

from scripts import run_exam


def test_exam_compares_the_same_cases_and_keeps_errors_in_the_denominator(tmp_path, monkeypatch, capsys):
    exam = tmp_path / "exam.yaml"
    exam.write_text(
        "dataset: 对比\ncases:\n  - {query: 材质, products: [P001-002]}\n  - {query: 空题, empty: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["run_exam.py", "--user", "测试", "--exam", str(exam),
                                    "--input-price", "2", "--output-price", "4"])
    monkeypatch.setattr(run_exam, "getpass", lambda _: "测试密码")
    requests = []

    def mock_search(request):
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        query, mode = body["query"], body["mode"]
        requests.append((mode, query))
        assert body["include_candidates"]
        if mode == "semantic" and query == "空题":
            return httpx.Response(503)
        products = [{"product_code": "P001-002"}] if mode == "semantic" else []
        return httpx.Response(200, json={
            "mode": mode, "products": products, "projects": [],
            "candidate_codes": ["P001-002"] if query == "材质" else [],
            "project_candidate_codes": [], "degraded": mode == "semantic",
            "usage": [{"input_tokens": 100, "output_tokens": 50}] if mode == "semantic" else [],
        })

    make_client = httpx.Client
    monkeypatch.setattr(run_exam.httpx, "Client", lambda **options: make_client(transport=httpx.MockTransport(mock_search), **options))
    assert run_exam.main() == 1
    assert requests == [("quick", "材质"), ("quick", "空题"), ("semantic", "材质"), ("semantic", "空题")]
    result = json.loads(capsys.readouterr().out)["modes"]
    quick, semantic = result["quick"], result["semantic"]
    assert quick["candidate_recall"] == 1 and quick["top10_hit_rate"] == 0
    assert quick["misses"][0]["reason"] == "进入候选但未进入前十"
    assert quick["empty_accuracy"] == 1 and quick["ai_calls"] == 0
    assert semantic["top10_hit_rate"] == 1 and semantic["degraded_queries"] == semantic["errors"] == 1
    assert semantic["empty_accuracy"] == 0 and semantic["ai_calls"] == 1
    assert semantic["estimated_cost"] == pytest.approx(0.0004)
