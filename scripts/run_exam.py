"""用人工标注考卷对比工作台的快速匹配和 AI 检索。"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from getpass import getpass
import json
import math
from pathlib import Path
import statistics
from time import perf_counter
from typing import Any, Literal

import httpx
import yaml

DEFAULT_EXAM = Path(__file__).resolve().parents[1] / "backend" / "tests" / "search_exam.yaml"
SearchMode = Literal["quick", "semantic"]


def evaluate(cases: list[dict[str, Any]], search: Callable[[str], dict[str, Any]], *,
             mode: SearchMode | None = None, input_price: float | None = None,
             output_price: float | None = None) -> dict[str, Any]:
    elapsed, misses = [], []
    correct = covered = measured = top3 = top10 = empty_ok = empty_total = errors = degraded = 0
    calls = prompt = completion = keyword = vector = 0
    for case in cases:
        query = case["query"]
        is_empty = bool(case.get("empty"))
        expected_products, expected_projects = set(case.get("products", [])), set(case.get("projects", []))
        truth = {*(("product", code) for code in expected_products), *(("project", code) for code in expected_projects)}
        if is_empty == bool(truth):
            raise ValueError("每道题须填写正确编号，或明确 empty")
        empty_total += int(is_empty)
        measured += int(not is_empty)
        correct += len(truth)

        begin = perf_counter()
        try:
            result = search(query)
            timing = (perf_counter() - begin) * 1000
            if mode is not None and (result["mode"] != mode or "project_candidate_codes" not in result):
                raise ValueError("检索模式或候选统计不匹配")
            products = [row["product_code"] for row in result["products"]]
            projects = [row["project_code"] for row in result["projects"]]
            candidates = {*(("product", code) for code in result["candidate_codes"]),
                          *(("project", code) for code in result.get("project_candidate_codes", projects))}
            usage = result.get("usage", [])
            input_tokens = sum(item["input_tokens"] for item in usage)
            output_tokens = sum(item["output_tokens"] for item in usage)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            errors += 1
            misses.append({"query": query, "reason": "接口错误或返回结构无效"})
            continue
        elapsed.append(timing)
        degraded += int(bool(result.get("degraded")))
        calls += len(usage)
        prompt += input_tokens
        completion += output_tokens
        keyword += result.get("keyword_candidates", 0)
        vector += result.get("vector_candidates", 0)

        if is_empty:
            empty_ok += int(not products and not projects)
            if products or projects:
                misses.append({"query": query, "reason": "预期无匹配，实际返回结果"})
            continue
        covered += len(truth & candidates)

        def hit(limit: int) -> bool:
            return bool(expected_products & set(products[:limit]) or expected_projects & set(projects[:limit]))

        top3 += int(hit(3))
        top10 += int(hit(10))
        if not hit(10):
            stage = "未进入候选" if not truth & candidates else "进入候选但未进入前十"
            misses.append({"query": query, "reason": stage, "warnings": result.get("warnings", [])})
        elif truth - candidates:
            misses.append({"query": query, "reason": "部分正确答案未进入候选",
                           "missing_codes": sorted(code for _, code in truth - candidates)})
    timings = sorted(elapsed)
    return {"cases": len(cases), "errors": errors, "degraded_queries": degraded,
            "candidate_recall": covered / correct if correct else None,
            "top3_hit_rate": top3 / measured if measured else None, "top10_hit_rate": top10 / measured if measured else None,
            "empty_correct": empty_ok, "empty_cases": empty_total, "empty_accuracy": empty_ok / empty_total if empty_total else None,
            "mean_ms": round(statistics.mean(timings), 2) if timings else None,
            "p95_ms": round(timings[math.ceil(len(timings) * 0.95) - 1], 2) if timings else None,
            "keyword_candidates": keyword, "vector_candidates": vector,
            "ai_calls": calls, "input_tokens": prompt, "output_tokens": completion,
            "estimated_cost": ((prompt * input_price + completion * output_price) / 1_000_000
                               if input_price is not None and output_price is not None else None),
            "misses": misses}


def main() -> int:
    parser = argparse.ArgumentParser(description="运行设计资产双模式搜索考卷")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--user", required=True)
    parser.add_argument("--exam", type=Path, default=DEFAULT_EXAM)
    parser.add_argument("--mode", choices=("both", "quick", "semantic"), default="both", help="双模式对比、仅快速匹配或仅 AI 搜索")
    parser.add_argument("--input-price", type=float, help="每百万输入 token 的统一估算单价")
    parser.add_argument("--output-price", type=float, help="每百万输出 token 的统一估算单价")
    options = parser.parse_args()
    document = yaml.safe_load(options.exam.read_text(encoding="utf-8"))
    modes: tuple[SearchMode, ...] = ("quick", "semantic") if options.mode == "both" else (options.mode,)
    summaries: dict[SearchMode, dict[str, Any]] = {}
    with httpx.Client(base_url=options.url, timeout=120, trust_env=False) as client:
        response = client.post("/api/auth/login", json={"username": options.user, "password": getpass("登录密码：")})
        if response.status_code != 200:
            parser.exit(1, "登录失败，请核对账号和服务地址\n")

        def search(query: str, mode: SearchMode) -> dict:
            result = client.post("/api/search", json={"query": query, "mode": mode, "include_candidates": True})
            result.raise_for_status()
            return result.json()

        for mode in modes:
            summaries[mode] = evaluate(document["cases"], lambda query: search(query, mode), mode=mode,
                                       input_price=options.input_price, output_price=options.output_price)
    print(json.dumps({"dataset": document.get("dataset", "未命名考卷"), "modes": summaries}, ensure_ascii=False, indent=2))
    invalid = any(summary["errors"] or (mode == "quick" and summary["ai_calls"]) for mode, summary in summaries.items())
    return int(invalid)


if __name__ == "__main__":
    raise SystemExit(main())
