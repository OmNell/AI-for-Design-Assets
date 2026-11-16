#!/usr/bin/env python3
"""对正在运行的工作台跑检索考卷，输出海选命中率、前 3 / 前 10 命中率、平均耗时和模型调用次数。

    python scripts/run_exam.py --base-url http://localhost:8080 --username 你的账号

密码不写在命令行里时会提示输入；也可以用环境变量 NASWB_EXAM_PASSWORD。考卷格式见 backend/tests/search_exam.yaml。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import sys

import httpx
import yaml

DEFAULT_EXAM = Path(__file__).resolve().parents[1] / "backend" / "tests" / "search_exam.yaml"
TYPES = ("编号", "属性", "场景")
PAGE_LIMIT = 3  # 每页 20 条，候选最多 60 条


@dataclass
class Outcome:
    query: str
    kind: str
    answers: list[str]
    rank: int | None        # 第一个正确答案的名次（从 1 起）；没进候选为 None
    elapsed_ms: int
    local_ms: int
    model_calls: int
    degraded: bool


def load_exam(path: Path) -> list[dict[str, object]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = document.get("exam") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SystemExit("考卷里没有题目：需要顶层的 exam 列表")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("query"), str) or not row["query"].strip():
            raise SystemExit(f"第 {index} 题缺少 query")
        if row.get("type") not in TYPES:
            raise SystemExit(f"第 {index} 题的 type 必须是 {' / '.join(TYPES)}")
        if not isinstance(row.get("answers"), list) or not row["answers"] or not all(isinstance(value, str) for value in row["answers"]):
            raise SystemExit(f"第 {index} 题的 answers 必须是非空的商品编号列表")
    return rows


def first_rank(codes: list[str], answers: list[str]) -> int | None:
    wanted = {value.strip().upper() for value in answers}
    return next((index for index, code in enumerate(codes, 1) if code.upper() in wanted), None)


def summarize(outcomes: list[Outcome]) -> dict[str, object]:
    total = len(outcomes)

    def rate(limit: int | None, rows: list[Outcome]) -> float:
        return sum(1 for row in rows if row.rank is not None and (limit is None or row.rank <= limit)) / len(rows) if rows else 0.0

    return {"total": total, "recall": rate(None, outcomes), "top3": rate(3, outcomes), "top10": rate(10, outcomes),
            "avg_ms": sum(row.elapsed_ms for row in outcomes) / total if total else 0.0,
            "avg_local_ms": sum(row.local_ms for row in outcomes) / total if total else 0.0,
            "model_calls": sum(row.model_calls for row in outcomes), "degraded": sum(row.degraded for row in outcomes),
            "by_type": {kind: {"total": len(rows), "recall": rate(None, rows), "top3": rate(3, rows), "top10": rate(10, rows)}
                        for kind in TYPES if (rows := [row for row in outcomes if row.kind == kind])},
            "missed": [row for row in outcomes if row.rank is None], "low": [row for row in outcomes if row.rank is not None and row.rank > 10]}


def run(client: httpx.Client, rows: list[dict[str, object]]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for row in rows:
        codes: list[str] = []
        first: dict[str, object] = {}
        for page in range(1, PAGE_LIMIT + 1):
            response = client.post("/api/search", json={"query": row["query"], "page": page})
            response.raise_for_status()
            body = response.json()
            first = first or body
            codes += [item["product_code"] for item in body["results"]]
            if len(codes) >= body["total"]:
                break
        outcomes.append(Outcome(query=str(row["query"]), kind=str(row["type"]), answers=list(row["answers"]), rank=first_rank(codes, list(row["answers"])),
                                elapsed_ms=int(first["elapsed_ms"]), local_ms=int(first["local_ms"]), model_calls=int(first["model_calls"]), degraded=bool(first["degraded"])))
    return outcomes


def report(summary: dict[str, object]) -> str:
    percent = lambda value: f"{value * 100:5.1f}%"  # noqa: E731
    lines = [f"题目数            {summary['total']}",
             f"海选命中率        {percent(summary['recall'])}   （正确答案进入候选）",
             f"前 3 命中率       {percent(summary['top3'])}",
             f"前 10 命中率      {percent(summary['top10'])}",
             f"平均耗时          {summary['avg_ms']:.0f} 毫秒（其中本地环节 {summary['avg_local_ms']:.0f} 毫秒）",
             f"模型调用次数      {summary['model_calls']}（降级 {summary['degraded']} 次）", "", "按类型："]
    for kind, row in summary["by_type"].items():
        lines.append(f"  {kind}  {row['total']:3d} 题   海选 {percent(row['recall'])}   前 3 {percent(row['top3'])}   前 10 {percent(row['top10'])}")
    lines += ["", f"未进入候选的查询（{len(summary['missed'])}）："]
    lines += [f"  [{row.kind}] {row.query}  → 期望 {'、'.join(row.answers)}" for row in summary["missed"]] or ["  （无）"]
    lines += ["", f"进了候选但排在 10 名之后（{len(summary['low'])}）："]
    lines += [f"  [{row.kind}] {row.query}  → 第 {row.rank} 名" for row in summary["low"]] or ["  （无）"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑检索考卷")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--username", required=True)
    parser.add_argument("--exam", type=Path, default=DEFAULT_EXAM)
    args = parser.parse_args(argv)
    rows = load_exam(args.exam)
    password = os.environ.get("NASWB_EXAM_PASSWORD") or getpass.getpass("NAS 密码：")
    with httpx.Client(base_url=args.base_url, timeout=180) as client:
        login = client.post("/api/auth/login", json={"username": args.username, "password": password})
        if login.status_code != 200:
            print("登录失败：", login.json().get("message", login.status_code), file=sys.stderr)
            return 2
        try:
            outcomes = run(client, rows)
        finally:
            client.post("/api/auth/logout")
    print(report(summarize(outcomes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
