"""离线回放三方对比：向量基线 / Jev 判定 / 人工标注。

脚本不进产品链路，只用于调参与回归：把真实检索导出的候选小节写成 JSONL，
离线重放两种排序口径，并与人工标注对比。输入每行一个主题：

    {"case_id": "c1", "topic": "泛化能力", "aliases": [],
     "lecture_summary": "本节讨论泛化能力",
     "expected_section": "s-11",
     "candidates": [{"section_id": "s-1", "section_title": "1.1 基本概念",
                     "section_path": "第1章 绪论 > 1.1 基本概念", "level": 2,
                     "excerpt": "……", "vector_score": 0.91}]}

用法：
    uv run python scripts/compare_mapping_judges.py --cases cases.jsonl
    TEACH_JEV_API_KEY=... uv run python scripts/compare_mapping_judges.py --cases cases.jsonl --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from teach_agent.config import JEV_API_KEY, JEV_BASE_URL, JEV_MODEL, JEV_TIMEOUT
from teach_agent.curriculum.jev import request_systemone
from teach_agent.curriculum.mapping import (
    JUDGE_TOP_K,
    build_section_fit_probe,
    parse_section_fits,
)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or not case.get("candidates"):
            raise ValueError(f"第 {line_number} 行缺少 candidates")
        cases.append(case)
    return cases


def _vector_pick(candidates: list[dict[str, Any]]) -> str:
    return max(candidates, key=lambda candidate: float(candidate["vector_score"]))["section_id"]


def _jev_pick(case: dict[str, Any]) -> tuple[str | None, str]:
    ranked = sorted(
        case["candidates"],
        key=lambda candidate: float(candidate["vector_score"]),
        reverse=True,
    )[:JUDGE_TOP_K]
    probe_candidates = [
        {
            "candidate_id": f"candidate_{index}",
            "section_title": candidate["section_title"],
            "section_path": candidate.get("section_path", candidate["section_title"]),
            "level": candidate.get("level", 1),
            "excerpt": candidate.get("excerpt", ""),
        }
        for index, candidate in enumerate(ranked)
    ]
    state, questions = build_section_fit_probe(
        topic=case["topic"],
        aliases=case.get("aliases", []),
        lecture_summary=case.get("lecture_summary"),
        candidates=probe_candidates,
    )
    answers = request_systemone(
        state,
        questions,
        api_key=JEV_API_KEY,
        model=JEV_MODEL,
        base_url=JEV_BASE_URL,
        timeout=JEV_TIMEOUT,
    )
    fits = parse_section_fits(answers, len(ranked))
    if fits is None:
        return None, "fallback"
    ordered = sorted(
        zip(ranked, fits, strict=True),
        key=lambda pair: (pair[1], float(pair[0]["vector_score"])),
        reverse=True,
    )
    if ordered[0][1] == 0:
        return None, "zero_fit"
    return ordered[0][0]["section_id"], "ok"


def _accuracy(rows: list[dict[str, Any]], key: str) -> float | None:
    graded = [row for row in rows if row["expected_section"] is not None]
    if not graded:
        return None
    return round(sum(row[key] == row["expected_section"] for row in graded) / len(graded), 3)


def main() -> int:
    parser = argparse.ArgumentParser(description="映射判定器离线回放对比")
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="只输出 JSON 汇总")
    args = parser.parse_args()

    cases = _load_cases(args.cases)
    rows: list[dict[str, Any]] = []
    jev_available = bool(JEV_API_KEY)
    for case in cases:
        vector_pick = _vector_pick(case["candidates"])
        jev_pick, jev_status = _jev_pick(case) if jev_available else (None, "skipped")
        rows.append(
            {
                "case_id": case.get("case_id", case["topic"]),
                "topic": case["topic"],
                "vector_pick": vector_pick,
                "jev_pick": jev_pick,
                "jev_status": jev_status,
                "expected_section": case.get("expected_section"),
            }
        )

    summary = {
        "cases": len(rows),
        "vector_accuracy": _accuracy(rows, "vector_pick"),
        "jev_accuracy": _accuracy(rows, "jev_pick") if jev_available else None,
        "disagreements": [
            row["case_id"]
            for row in rows
            if row["jev_pick"] is not None and row["jev_pick"] != row["vector_pick"]
        ],
        "rows": rows,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not jev_available:
        print("警告：未配置 TEACH_JEV_API_KEY/TYPESAFE_API_KEY，仅回放向量基线。")
    for row in rows:
        print(
            f"{row['case_id']:<16} vector={row['vector_pick']:<12} "
            f"jev={str(row['jev_pick']):<12} ({row['jev_status']}) "
            f"expected={row['expected_section']}"
        )
    print(
        f"共 {summary['cases']} 例；向量准确率={summary['vector_accuracy']}；"
        f"Jev 准确率={summary['jev_accuracy']}；分歧={len(summary['disagreements'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
