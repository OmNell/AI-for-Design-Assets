"""用人工标注考卷运行当前工作台的搜索接口。"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from getpass import getpass
import json
import math
from pathlib import Path
import statistics
from time import perf_counter
from typing import Any

import httpx
import yaml

DEFAULT_EXAM = Path(__file__).resolve().parents[1] / "backend" / "tests" / "search_exam.yaml"


def evaluate(cases: list[dict[str, Any]], search: Callable[[str], dict[str, Any]], *,
             input_price: float | None = None, output_price: float | None = None) -> dict[str, Any]:
    elapsed, misses = [], []
    correct = covered = measured = top3 = top10 = empty_ok = empty_total = errors = 0
    calls = prompt = completion = 0
    for case in cases:
        query = case["query"]
        begin = perf_counter()
        try:
            result = search(query)
        except (httpx.HTTPError, ValueError, KeyError):
            errors += 1
            misses.append({"query": query, "reason": "接口错误或返回结构无效"})
            continue
        elapsed.append((perf_counter() - begin) * 1000)
        for item in result.get("usage", []):
            calls += 1
            prompt += item["input_tokens"]
            completion += item["output_tokens"]
        products = [row["product_code"] for row in result["products"]]
        projects = [row["project_code"] for row in result["projects"]]
        if case.get("empty"):
            empty_total += 1
            empty_ok += int(not products and not projects)
            if products or projects:
                misses.append({"query": query, "reason": "预期无匹配，实际返回候选"})
            continue
        expected_products, expected_projects = set(case.get("products", [])), set(case.get("projects", []))
        truth = {*(("product", code) for code in expected_products), *(("project", code) for code in expected_projects)}
        if not truth:
            raise ValueError("每道题须填写正确编号，或明确 empty")
        candidates = {*(("product", code) for code in result.get("candidate_codes", [])),
                      *(("project", code) for code in projects)}
        measured += 1
        correct += len(truth)
        covered += len(truth & candidates)

        def hit(limit: int) -> bool:
            return bool(expected_products & set(products[:limit]) or expected_projects & set(projects[:limit]))

        top3 += int(hit(3))
        top10 += int(hit(10))
        if not hit(10):
            warnings = result.get("warnings", [])
            stage = "未进入候选" if not truth & candidates else "候选未进入前十"
            misses.append({"query": query, "reason": stage, "warnings": warnings})
        elif truth - candidates:
            misses.append({"query": query, "reason": "部分正确答案未进入候选",
                           "missing_codes": sorted(code for _, code in truth - candidates)})
    timings = sorted(elapsed)
    return {"cases": len(cases), "errors": errors, "candidate_recall": covered / correct if correct else None,
            "top3_hit_rate": top3 / measured if measured else None, "top10_hit_rate": top10 / measured if measured else None,
            "empty_correct": empty_ok, "empty_cases": empty_total, "mean_ms": round(statistics.mean(timings), 2) if timings else None,
            "p95_ms": round(timings[math.ceil(len(timings) * 0.95) - 1], 2) if timings else None,
            "ai_calls": calls, "input_tokens": prompt, "output_tokens": completion,
            "estimated_cost": ((prompt * input_price + completion * output_price) / 1_000_000
                               if input_price is not None and output_price is not None else None),
            "misses": misses}


def main() -> int:
    parser = argparse.ArgumentParser(description="运行设计资产搜索考卷")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--user", required=True)
    parser.add_argument("--exam", type=Path, default=DEFAULT_EXAM)
    parser.add_argument("--input-price", type=float, help="每百万输入 token 的统一估算单价")
    parser.add_argument("--output-price", type=float, help="每百万输出 token 的统一估算单价")
    options = parser.parse_args()
    document = yaml.safe_load(options.exam.read_text(encoding="utf-8"))
    with httpx.Client(base_url=options.url, timeout=120, trust_env=False) as client:
        response = client.post("/api/auth/login", json={"username": options.user, "password": getpass("登录密码：")})
        if response.status_code != 200:
            parser.exit(1, "登录失败，请核对账号和服务地址\n")

        def search(query: str) -> dict:
            result = client.post("/api/search", json={"query": query})
            result.raise_for_status()
            return result.json()

        summary = evaluate(document["cases"], search, input_price=options.input_price, output_price=options.output_price)
    print(json.dumps({"dataset": document.get("dataset", "未命名考卷"), **summary}, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
