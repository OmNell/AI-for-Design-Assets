"""考卷脚本的读取与统计。"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("run_exam", ROOT / "scripts" / "run_exam.py")
run_exam = importlib.util.module_from_spec(spec)
import sys
sys.modules["run_exam"] = run_exam
spec.loader.exec_module(run_exam)


def test_bundled_exam_has_ten_valid_examples_of_three_types() -> None:
    rows = run_exam.load_exam(ROOT / "backend" / "tests" / "search_exam.yaml")
    assert len(rows) == 10 and {row["type"] for row in rows} == {"编号", "属性", "场景"}
    assert all(row["answers"] for row in rows)


def test_invalid_exam_is_rejected(tmp_path: Path) -> None:
    for text in ("exam: []", "exam:\n  - query: x\n    type: 别的\n    answers: [a]", "exam:\n  - query: x\n    type: 编号\n    answers: []", "other: 1"):
        path = tmp_path / "exam.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit):
            run_exam.load_exam(path)


def test_rank_and_summary_metrics() -> None:
    assert run_exam.first_rank(["A-1", "A-2", "A-3"], ["a-3", "A-9"]) == 3 and run_exam.first_rank(["A-1"], ["B-1"]) is None
    outcome = lambda kind, rank, calls=3: run_exam.Outcome("q", kind, ["x"], rank, 100, 20, calls, False)  # noqa: E731
    summary = run_exam.summarize([outcome("编号", 1, 0), outcome("属性", 3), outcome("属性", 12), outcome("场景", None)])
    assert (summary["recall"], summary["top3"], summary["top10"]) == (0.75, 0.5, 0.5)
    assert summary["model_calls"] == 9 and summary["avg_ms"] == 100 and summary["avg_local_ms"] == 20
    assert summary["by_type"]["属性"] == {"total": 2, "recall": 1.0, "top3": 0.5, "top10": 0.5}
    assert len(summary["missed"]) == 1 and len(summary["low"]) == 1
    text = run_exam.report(summary)
    assert "海选命中率         75.0%" in text and "未进入候选的查询（1）" in text and "第 12 名" in text
