"""课堂主题到课程绑定教材小节的可追溯映射。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from teach_agent.db.models import (
    BookSection,
    BookSectionChunk,
    CourseBook,
    Lecture,
    LectureSegment,
    TopicMapping,
)
from teach_agent.retriever import search
from teach_agent.schema import RetrievedChunk

from .jev import Judge

MAPPER_VERSION = "section-map-v2"
MAPPING_TOP_K = 12
MAPPING_DISTANCE_THRESHOLD = 0.65
JUDGE_TOP_K = 6
# Jev Score 的 3 档序数量表：0 = 未讲该主题，2 = 直接讲该主题。
JUDGE_CRITERIA = ("没有讲该主题", "相关但不是同一主题", "直接讲该主题")
REVIEW_PROTECTED_STATUSES = frozenset({"approved", "rejected"})
Retriever = Callable[[str, str | None, int, float], tuple[list[RetrievedChunk], str | None]]
Candidate = tuple[RetrievedChunk, BookSection, float]


@dataclass(frozen=True)
class TopicInput:
    topic: str
    relation_type: str = "direct"
    retrieval_aliases: tuple[str, ...] = ()
    lecture_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MappingItem:
    mapping_id: str
    topic: str
    section_id: str | None
    section_title: str | None
    confidence: float | None
    lecture_evidence_ids: tuple[str, ...]
    book_evidence_ids: tuple[str, ...]
    review_status: str


@dataclass(frozen=True)
class MappingResult:
    lecture_id: str
    mappings: tuple[MappingItem, ...]


@dataclass(frozen=True)
class MappingReviewResult:
    mapping_id: str
    review_status: str


def _mapping_id(lecture_id: str, topic: str) -> str:
    identity = f"{MAPPER_VERSION}|{lecture_id}|{topic.casefold()}"
    return "mapping_" + hashlib.sha256(identity.encode()).hexdigest()[:48]


def _title_boost(topic: str, section_title: str) -> float:
    title_text = re.sub(r"^(?:第\d+章|\d+(?:\.\d+)*)\s*", "", section_title).strip()
    if topic == title_text:
        return 0.30
    if len(topic) >= 3 and (topic in title_text or title_text in topic):
        return 0.18
    return 0.0


def _group_score(group: Sequence[Candidate]) -> float:
    """同一小节的多段命中取最高向量分，并给多段命中少量聚合奖励。"""
    return max(candidate[2] for candidate in group) + 0.04 * min(len(group) - 1, 3)


def _section_paths(engine: Engine, sections: Sequence[BookSection]) -> dict[str, str]:
    """批量取候选小节的祖先标题，生成便于判定的章节路径。"""
    unique = {section.id: section for section in sections}
    if not unique:
        return {}
    rows: list[BookSection] = []
    with Session(engine) as session:
        for book_id, index_version in {
            (section.book_id, section.index_version) for section in unique.values()
        }:
            rows.extend(
                session.scalars(
                    select(BookSection).where(
                        BookSection.book_id == book_id,
                        BookSection.index_version == index_version,
                    )
                ).all()
            )
    by_id = {row.id: row for row in rows}
    paths: dict[str, str] = {}
    for section_id in unique:
        titles: list[str] = []
        current: BookSection | None = by_id.get(section_id)
        while current is not None and len(titles) < 20:
            titles.append(current.title)
            current = by_id.get(current.parent_id) if current.parent_id else None
        paths[section_id] = " > ".join(reversed(titles)) or unique[section_id].title
    return paths


def build_section_fit_probe(
    *,
    topic: str,
    aliases: Sequence[str],
    lecture_summary: str | None,
    candidates: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造小节匹配判定所需的 state 与 Score 问题包（CLI 与回放脚本共用）。"""
    state = {
        "topic": topic,
        "aliases": list(aliases),
        "lecture_summary": (lecture_summary or "")[:300],
        "candidates": list(candidates),
    }
    questions = {
        f"candidate_{index}": {
            "type": "score",
            "instructions": {
                "question": (
                    "这个教材小节实际讲授的内容与课堂主题的匹配程度如何？"
                    "只有正文主体就在讲该主题时才判为“直接讲”；仅仅同属一章、"
                    "或只在标题里出现相似词，都不算。"
                ),
                "topic": topic,
                "section_title": candidate["section_title"],
                "section_path": candidate.get("section_path"),
                "excerpt": candidate.get("excerpt"),
            },
            "criteria": list(JUDGE_CRITERIA),
        }
        for index, candidate in enumerate(candidates)
    }
    return state, questions


def parse_section_fits(answers: object, count: int) -> list[int] | None:
    """校验并归一化判定器返回的 Score：缺项、类型不符或越界时整体回落。"""
    if not isinstance(answers, dict):
        return None
    max_level = len(JUDGE_CRITERIA) - 1
    fits: list[int] = []
    for index in range(count):
        answer = answers.get(f"candidate_{index}")
        if not isinstance(answer, dict):
            return None
        score = answer.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        if not 0.0 <= float(score) <= max_level + 0.01:
            return None
        fits.append(max(0, min(max_level, round(float(score)))))
    return fits


def _ask_section_fit(
    judge: Judge,
    *,
    topic: str,
    aliases: Sequence[str],
    lecture_summary: str | None,
    groups: Sequence[Sequence[Candidate]],
    section_paths: dict[str, str],
) -> list[int] | None:
    """向判定器询问每个候选小节的匹配等级；结构不符时整体回落。"""
    candidates = [
        {
            "candidate_id": f"candidate_{index}",
            "section_title": group[0][1].title,
            "section_path": section_paths.get(group[0][1].id, group[0][1].title),
            "level": group[0][1].level,
            "excerpt": " ".join(group[0][0].text.split())[:500],
        }
        for index, group in enumerate(groups)
    ]
    state, questions = build_section_fit_probe(
        topic=topic,
        aliases=aliases,
        lecture_summary=lecture_summary,
        candidates=candidates,
    )
    return parse_section_fits(judge(state, questions), len(groups))


def map_topics(
    engine: Engine,
    lecture_id: str,
    topics: Sequence[TopicInput],
    *,
    retrieve: Retriever = search,
    top_k: int = MAPPING_TOP_K,
    score_threshold: float = MAPPING_DISTANCE_THRESHOLD,
    judge: Judge | None = None,
    judge_top_k: int = JUDGE_TOP_K,
) -> MappingResult:
    """把人工确认或上游抽取的主题映射到课程绑定教材的小节。

    检索范围由数据库中的课程—教材绑定强制决定。结果始终保存为待审核，
    相似度仅用于排序和诊断，不被解释为映射正确概率。

    ``judge`` 为可选的外部判定器（签名见 ``curriculum.jev.Judge``）：它只
    重排向量召回的候选小节，不扩大候选范围；超时、报错或结构不符时自动
    回落到向量排序。人工已审核（approved/rejected）的映射不会被重排覆盖。
    """
    if judge_top_k < 1 or judge_top_k > 16:
        raise ValueError("judge_top_k 必须为 1..16")
    normalized: list[TopicInput] = []
    seen: set[str] = set()
    for item in topics:
        topic = " ".join(item.topic.split())
        if not topic or len(topic) > 500:
            raise ValueError("topic 长度必须为 1..500")
        if item.relation_type not in {"direct", "prerequisite", "extension"}:
            raise ValueError("relation_type 必须是 direct/prerequisite/extension")
        aliases = tuple(
            dict.fromkeys(
                alias
                for raw_alias in item.retrieval_aliases
                if (alias := " ".join(raw_alias.split())) and alias.casefold() != topic.casefold()
            )
        )
        if any(len(alias) > 500 for alias in aliases):
            raise ValueError("retrieval_aliases 中每个别名长度必须为 1..500")
        lecture_evidence_ids = tuple(
            dict.fromkeys(
                evidence_id.removeprefix("lecture:")
                for evidence_id in item.lecture_evidence_ids
                if evidence_id.removeprefix("lecture:")
            )
        )
        key = topic.casefold()
        if key not in seen:
            normalized.append(
                TopicInput(
                    topic=topic,
                    relation_type=item.relation_type,
                    retrieval_aliases=aliases,
                    lecture_evidence_ids=lecture_evidence_ids,
                )
            )
            seen.add(key)
    if not normalized:
        raise ValueError("至少需要一个主题")

    with Session(engine) as session:
        lecture = session.get(Lecture, lecture_id)
        if lecture is None:
            raise ValueError("课次不存在")
        evidence_rows = session.scalars(
            select(LectureSegment)
            .where(LectureSegment.lecture_id == lecture_id)
            .order_by(LectureSegment.evidence_type, LectureSegment.ordinal)
        ).all()
        bindings = session.scalars(
            select(CourseBook).where(CourseBook.course_id == lecture.course_id)
        ).all()
        if not bindings:
            raise ValueError("课程尚未绑定教材")
        if any(not binding.book_version for binding in bindings):
            raise ValueError("绑定教材尚未构建章节目录")

    with Session(engine) as session:
        existing_rows = session.scalars(
            select(TopicMapping)
            .where(TopicMapping.lecture_id == lecture_id)
            .order_by(TopicMapping.updated_at.desc(), TopicMapping.id)
        ).all()
    existing_by_topic: dict[str, TopicMapping] = {}
    protected_by_topic: dict[str, TopicMapping] = {}
    for row in existing_rows:
        key = row.topic.casefold()
        existing_by_topic.setdefault(key, row)
        if row.review_status in REVIEW_PROTECTED_STATUSES:
            protected_by_topic.setdefault(key, row)

    output: list[MappingItem] = []
    evidence_by_id = {row.id: row for row in evidence_rows}
    for item in normalized:
        protected = protected_by_topic.get(item.topic.casefold())
        if protected is not None:
            with Session(engine) as session:
                section = (
                    session.get(BookSection, protected.section_id)
                    if protected.section_id
                    else None
                )
            output.append(
                MappingItem(
                    mapping_id=protected.id,
                    topic=protected.topic,
                    section_id=protected.section_id,
                    section_title=section.title if section else None,
                    confidence=(
                        round(float(protected.confidence), 4)
                        if protected.confidence is not None
                        else None
                    ),
                    lecture_evidence_ids=tuple(json.loads(protected.lecture_evidence_ids)),
                    book_evidence_ids=tuple(json.loads(protected.book_evidence_ids)),
                    review_status=protected.review_status,
                )
            )
            continue

        if item.lecture_evidence_ids:
            unknown = set(item.lecture_evidence_ids) - evidence_by_id.keys()
            if unknown:
                raise ValueError(
                    f"主题“{item.topic}”引用了不属于该课次的 evidence："
                    + "、".join(sorted(unknown))
                )
            matching_evidence = [
                evidence_by_id[evidence_id] for evidence_id in item.lecture_evidence_ids
            ]
        else:
            matching_evidence = [row for row in evidence_rows if item.topic in row.text]
            if not matching_evidence:
                matching_evidence = evidence_rows
        context_parts = [item.topic]
        if lecture.summary:
            context_parts.append(lecture.summary[:300])
        context_parts.extend(row.text[:500] for row in matching_evidence[:3])
        retrieval_query = "\n".join(dict.fromkeys(context_parts))

        candidates: list[tuple[RetrievedChunk, BookSection, float]] = []
        reasons: list[str] = []
        for binding in bindings:
            hits_by_id: dict[str, RetrievedChunk] = {}
            for query in dict.fromkeys((item.topic, *item.retrieval_aliases, retrieval_query)):
                hits, reason = retrieve(query, binding.book_id, top_k, score_threshold)
                if reason:
                    reasons.append(reason)
                for hit in hits:
                    previous = hits_by_id.get(hit.chunk_id)
                    if previous is None or hit.distance < previous.distance:
                        hits_by_id[hit.chunk_id] = hit
            hits = list(hits_by_id.values())
            if not hits:
                continue
            hit_ids = [hit.chunk_id for hit in hits]
            with Session(engine) as session:
                section_rows = session.execute(
                    select(BookSectionChunk.chunk_id, BookSection)
                    .join(BookSection, BookSection.id == BookSectionChunk.section_id)
                    .where(
                        BookSection.book_id == binding.book_id,
                        BookSection.index_version == binding.book_version,
                        BookSectionChunk.chunk_id.in_(hit_ids),
                    )
                ).all()
            by_chunk = {chunk_id: section for chunk_id, section in section_rows}
            title_terms = (item.topic, *item.retrieval_aliases)
            candidates.extend(
                (
                    hit,
                    by_chunk[hit.chunk_id],
                    hit.score
                    + max(_title_boost(term, by_chunk[hit.chunk_id].title) for term in title_terms),
                )
                for hit in hits
                if hit.chunk_id in by_chunk
            )

        by_section: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            by_section.setdefault(candidate[1].id, []).append(candidate)
        group_rank = sorted(by_section.values(), key=_group_score, reverse=True)
        judge_used = False
        judge_payload: dict[str, Any] | None = None
        judged_top_fit: int | None = None
        if judge is not None and group_rank:
            evaluated = group_rank[:judge_top_k]
            fits = _ask_section_fit(
                judge,
                topic=item.topic,
                aliases=item.retrieval_aliases,
                lecture_summary=lecture.summary,
                groups=evaluated,
                section_paths=_section_paths(engine, [group[0][1] for group in evaluated]),
            )
            if fits is None:
                reasons.append("jev_fallback")
            else:
                judge_used = True
                ranked_groups = sorted(
                    zip(evaluated, fits, strict=True),
                    key=lambda pair: (pair[1], _group_score(pair[0])),
                    reverse=True,
                )
                judged_top_fit = ranked_groups[0][1]
                if (
                    judged_top_fit > 0
                    and len(ranked_groups) > 1
                    and ranked_groups[0][1] == ranked_groups[1][1]
                ):
                    reasons.append("tie_break=vector")
                group_rank = [group for group, _ in ranked_groups] + group_rank[judge_top_k:]
                judge_payload = {
                    "status": "used",
                    "criteria": list(JUDGE_CRITERIA),
                    "fits": [
                        {"section_id": group[0][1].id, "fit": fit}
                        for group, fit in ranked_groups
                    ],
                }
        if judge is not None and judge_payload is None:
            judge_payload = {"status": "fallback"}

        if not group_rank or judged_top_fit == 0:
            best_hit, best_section, best_group = None, None, None
        else:
            best_group = group_rank[0]
            winning = sorted(best_group, key=lambda candidate: candidate[2], reverse=True)
            best_hit, best_section, _ = winning[0]
        if best_group is not None:
            remaining = [
                candidate for candidate in candidates if candidate[1].id != best_section.id
            ]
            remaining.sort(key=lambda candidate: candidate[2], reverse=True)
            ranked_candidates = [*winning, *remaining]
        else:
            ranked_candidates = []
        lecture_evidence_ids = tuple(f"lecture:{row.id}" for row in matching_evidence)
        book_evidence_ids = (
            tuple(f"book:{hit.chunk_id}" for hit, _, _ in ranked_candidates[:3])
            if ranked_candidates
            else ()
        )
        confidence = max(0.0, min(0.9999, best_hit.score)) if best_hit else None
        review_status = "pending" if best_section else "needs_review"
        fingerprint_payload = {
            "lecture_revision": lecture.source_revision,
            "topic": item.topic,
            "retrieval_aliases": item.retrieval_aliases,
            "lecture_evidence_ids": item.lecture_evidence_ids,
            "relation_type": item.relation_type,
            "retrieval_query": retrieval_query,
            "bindings": sorted((binding.book_id, binding.book_version) for binding in bindings),
            "book_evidence_ids": book_evidence_ids,
            "reasons": reasons,
            "mapping_judge": (
                "jev" if judge_used else "jev_fallback" if judge is not None else "vector"
            ),
            "judge": judge_payload,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        existing = existing_by_topic.get(item.topic.casefold())
        mapping_id = existing.id if existing is not None else _mapping_id(lecture_id, item.topic)

        with Session(engine) as session, session.begin():
            mapping = session.get(TopicMapping, mapping_id)
            if mapping is None:
                mapping = TopicMapping(id=mapping_id, lecture_id=lecture_id)
                session.add(mapping)
            mapping.topic = item.topic
            mapping.relation_type = item.relation_type
            mapping.lecture_evidence_ids = json.dumps(lecture_evidence_ids, ensure_ascii=False)
            mapping.book_evidence_ids = json.dumps(book_evidence_ids, ensure_ascii=False)
            mapping.section_id = best_section.id if best_section else None
            mapping.confidence = Decimal(f"{confidence:.4f}") if confidence is not None else None
            mapping.review_status = review_status
            mapping.mapper_version = MAPPER_VERSION
            mapping.input_fingerprint = fingerprint

        output.append(
            MappingItem(
                mapping_id=mapping_id,
                topic=item.topic,
                section_id=best_section.id if best_section else None,
                section_title=best_section.title if best_section else None,
                confidence=round(confidence, 4) if confidence is not None else None,
                lecture_evidence_ids=lecture_evidence_ids,
                book_evidence_ids=book_evidence_ids,
                review_status=review_status,
            )
        )

    return MappingResult(lecture_id=lecture_id, mappings=tuple(output))


def review_mapping(engine: Engine, mapping_id: str, review_status: str) -> MappingReviewResult:
    """记录人工映射审核；通过前强制检查章节和双侧 evidence。"""
    allowed = {"pending", "approved", "rejected", "needs_review"}
    if review_status not in allowed:
        raise ValueError("review_status 必须是 pending/approved/rejected/needs_review")
    with Session(engine) as session, session.begin():
        mapping = session.get(TopicMapping, mapping_id)
        if mapping is None:
            raise ValueError("主题映射不存在")
        if review_status == "approved":
            try:
                lecture_evidence = json.loads(mapping.lecture_evidence_ids)
                book_evidence = json.loads(mapping.book_evidence_ids)
            except json.JSONDecodeError as exc:
                raise ValueError("映射 evidence 不是有效 JSON") from exc
            evidence_is_valid = all(
                isinstance(values, list)
                and values
                and all(isinstance(item, str) for item in values)
                for values in (lecture_evidence, book_evidence)
            )
            if not mapping.section_id or not evidence_is_valid:
                raise ValueError("缺少章节或双侧 evidence 的映射不能审核通过")
        mapping.review_status = review_status
    return MappingReviewResult(mapping_id=mapping_id, review_status=review_status)
