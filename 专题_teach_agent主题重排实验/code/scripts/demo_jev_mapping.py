"""Jev 主题重排端到端 demo：真实 Jev + 内存课程库 + 固定候选。

演示四件事：
1. 向量基线如何选小节；
2. 真实 Jev 如何重排（或拒答）；
3. 人工已审核的映射不被重排覆盖；
4. 重排后的映射指纹与版本。

检索结果使用固定候选，保证离线可复现；只有判定环节走真实 Jev。
用法：
    uv run python scripts/demo_jev_mapping.py
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from teach_agent.config import JEV_API_KEY, JEV_BASE_URL, JEV_MODEL
from teach_agent.courses import LectureBundle, bind_book, import_bundle
from teach_agent.curriculum import TopicInput, build_book_outline, map_topics, review_mapping
from teach_agent.curriculum.jev import Answers, make_jev_judge
from teach_agent.curriculum.mapping import parse_section_fits
from teach_agent.db.models import Base, TopicMapping
from teach_agent.schema import RetrievedChunk

BOOK_ID = "book-demo"
LECTURE_ID = "lecture-demo"
RECORDED_PATH = Path(__file__).with_name("jev_demo_recorded.json")

TOPICS = (
    TopicInput("模型对未见样本的预测表现"),
    TopicInput("课程通知对应的背景知识"),
    TopicInput("校园网账号如何重置"),
)

# 章节与固定检索候选；distance 越小越相关。
# 2.3 习题课安排是“向量最像但内容无关”的干扰项，用来观察 Jev 是否纠正排序。
CHUNKS = (
    (
        "book-demo:00001",
        1,
        "第2章 模型评估",
        "本章讨论模型评估的目标、常用指标与评估流程。",
        0.40,
    ),
    (
        "book-demo:00002",
        2,
        "2.1 基本概念",
        "本章先介绍模型评估的基本概念，包括训练误差与测试误差的定义。",
        0.30,
    ),
    (
        "book-demo:00003",
        3,
        "2.2 泛化能力",
        "泛化能力指学习到的模型对未知样本的预测能力，是衡量模型是否可用的核心指标。",
        0.22,
    ),
    (
        "book-demo:00004",
        4,
        "2.2.1 泛化误差",
        "泛化误差可分解为偏差、方差与噪声之和，用于解释模型的欠拟合与过拟合。",
        0.18,
    ),
    (
        "book-demo:00005",
        5,
        "2.3 习题课安排",
        "本节安排习题课时间、答疑地点与作业提交要求。",
        0.05,
    ),
)


def _bundle() -> LectureBundle:
    return LectureBundle.model_validate(
        {
            "schema_version": 1,
            "source": "demo",
            "course_id": "course-demo",
            "lecture_id": LECTURE_ID,
            "source_revision": "demo-revision-1",
            "transcript_status": "complete",
            "segments": [
                {"segment_id": "s1", "text": "本节讨论模型评估、泛化能力与泛化误差分解。"},
                {"segment_id": "s2", "text": "补充课程通知。"},
            ],
        }
    )


def _write_chunks(directory: Path) -> Path:
    path = directory / "chunks.jsonl"
    rows = [
        {
            "chunk_id": chunk_id,
            "ordinal": ordinal,
            "chapter": chapter,
            "page_start": ordinal * 10,
            "page_end": ordinal * 10 + 1,
        }
        for chunk_id, ordinal, chapter, _, _ in CHUNKS
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _retrieve(query: str, book_id: str | None, top_k: int, threshold: float):
    del query, book_id, top_k, threshold
    return [
        RetrievedChunk(
            chunk_id=chunk_id,
            book_id=BOOK_ID,
            text=text,
            chapter=chapter,
            page_start=ordinal * 10,
            page_end=ordinal * 10 + 1,
            distance=distance,
        )
        for chunk_id, ordinal, chapter, text, distance in CHUNKS
    ], None


class RecordingJudge:
    """包一层真实判定器，记录每次的 state/questions/answers 与时延。

    本机代理链路偶发 connection reset，demo 最多重试两次；若仍失败，
    使用 ``jev_demo_recorded.json`` 中一次真实调用的响应回放，并标注来源，
    以免链路抖动让演示结果随机翻转。产品客户端本身不重试、不读回放。
    """

    def __init__(
        self,
        inner: Callable[[dict[str, Any], dict[str, Any]], Answers | None],
        recorded: dict[str, Answers] | None = None,
        attempts: int = 3,
    ) -> None:
        self.inner = inner
        self.recorded = recorded or {}
        self.attempts = attempts
        self.calls: list[dict[str, Any]] = []

    def __call__(self, state: dict[str, Any], questions: dict[str, Any]) -> Answers | None:
        started = time.perf_counter()
        answers = None
        tries = 0
        while tries < self.attempts and answers is None:
            tries += 1
            answers = self.inner(state, questions)
            if answers is None and tries < self.attempts:
                time.sleep(0.4)
        source = "live"
        if answers is None:
            answers = self.recorded.get(state.get("topic"))
            source = "recorded" if answers is not None else "failed"
        elapsed = round((time.perf_counter() - started) * 1000)
        self.calls.append(
            {
                "state": state,
                "questions": questions,
                "answers": answers,
                "elapsed_ms": elapsed,
                "tries": tries,
                "source": source,
            }
        )
        return answers


def _load_recorded() -> dict[str, Answers]:
    if not RECORDED_PATH.exists():
        return {}
    payload = json.loads(RECORDED_PATH.read_text(encoding="utf-8"))
    answers = payload.get("answers_by_topic")
    return answers if isinstance(answers, dict) else {}


def _setup(engine) -> str:
    imported = import_bundle(
        engine,
        _bundle(),
        student_id="student-demo",
        course_title="模型评估示范课",
    )
    bind_book(
        engine,
        student_id="student-demo",
        source="demo",
        source_course_id="course-demo",
        book_id=BOOK_ID,
    )
    with tempfile.TemporaryDirectory() as directory:
        build_book_outline(engine, BOOK_ID, chunks_path=_write_chunks(Path(directory)))
    return imported.lecture_id


def _print_mappings(title: str, result) -> None:
    print(f"\n== {title} ==")
    print(f"{'主题':<18} {'小节':<18} {'置信度':<8} {'状态':<12}")
    for item in result.mappings:
        print(
            f"{item.topic:<18} {str(item.section_title):<18} "
            f"{str(item.confidence):<8} {item.review_status:<12}"
        )


def _fingerprints(engine) -> dict[str, str]:
    with Session(engine) as session:
        rows = session.scalars(select(TopicMapping).order_by(TopicMapping.topic)).all()
        return {row.topic: row.input_fingerprint[:16] for row in rows}


def main() -> int:
    if not JEV_API_KEY:
        raise SystemExit("未配置 TEACH_JEV_API_KEY / TYPESAFE_API_KEY，无法运行真实 Jev 实验")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    lecture_id = _setup(engine)

    baseline = map_topics(engine, lecture_id, TOPICS, retrieve=_retrieve)
    _print_mappings("向量基线（judge=vector）", baseline)
    baseline_fingerprints = _fingerprints(engine)

    protected_topic = TOPICS[1].topic
    protected_id = baseline.mappings[1].mapping_id
    review_mapping(engine, protected_id, "approved")
    print(f"\n人工审核：{protected_topic} → {baseline.mappings[1].section_title}（approved）")

    judge = RecordingJudge(
        make_jev_judge(api_key=JEV_API_KEY, model=JEV_MODEL, base_url=JEV_BASE_URL),
        recorded=_load_recorded(),
    )
    judged = map_topics(engine, lecture_id, TOPICS, retrieve=_retrieve, judge=judge)
    _print_mappings("真实 Jev 重排（judge=jev）", judged)

    print(f"\n== Jev 判定明细（调用 {len(judge.calls)} 次）==")
    for call in judge.calls:
        topic = call["state"]["topic"]
        candidates = call["state"]["candidates"]
        fits = parse_section_fits(call["answers"], len(candidates))
        status = "ok" if fits is not None else "fallback"
        print(
            f"\n主题：{topic}  时延：{call['elapsed_ms']} ms  "
            f"尝试：{call['tries']}  来源：{call['source']}  状态：{status}"
        )
        if fits is None:
            print("  返回结构不符或调用失败，已回落到向量排序")
            continue
        for candidate, fit in zip(candidates, fits, strict=True):
            print(
                f"  {candidate['section_title']:<18} "
                f"path={candidate['section_path']:<44} fit={fit}"
            )

    protected = next(item for item in judged.mappings if item.topic == protected_topic)
    print("\n== 审核保护 ==")
    print(
        f"{protected_topic}：section={protected.section_title} "
        f"status={protected.review_status} id_unchanged={protected.mapping_id == protected_id}"
    )

    judged_fingerprints = _fingerprints(engine)
    print("\n== 映射指纹（前 16 位；变化即触发下游计划 revision）==")
    for topic in (item.topic for item in judged.mappings):
        before = baseline_fingerprints.get(topic, "-")
        after = judged_fingerprints.get(topic, "-")
        mark = "changed" if before != after else "unchanged"
        print(f"{topic:<18} {before} -> {after} ({mark})")

    with Session(engine) as session:
        versions = {row.mapper_version for row in session.scalars(select(TopicMapping)).all()}
    print(f"\nmapper_version={sorted(versions)}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
