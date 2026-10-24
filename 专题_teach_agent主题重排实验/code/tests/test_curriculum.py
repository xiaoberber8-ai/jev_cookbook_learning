from __future__ import annotations

import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from teach_agent.courses import LectureBundle, bind_book, import_bundle
from teach_agent.curriculum import TopicInput, build_book_outline, map_topics, review_mapping
from teach_agent.db.models import (
    Base,
    BookSection,
    BookSectionChunk,
    CourseBook,
    LectureSegment,
    TopicMapping,
)
from teach_agent.schema import RetrievedChunk


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _bundle() -> LectureBundle:
    return LectureBundle.model_validate(
        {
            "schema_version": 1,
            "source": "test",
            "course_id": "course-1",
            "lecture_id": "lecture-1",
            "source_revision": "revision-1",
            "transcript_status": "complete",
            "segments": [
                {"segment_id": "s1", "text": "本节讨论模型的泛化能力。"},
                {"segment_id": "s2", "text": "补充课程通知。"},
            ],
        }
    )


def _chunks(tmp_path):
    path = tmp_path / "chunks.jsonl"
    rows = [
        {
            "chunk_id": "book-1:00000",
            "ordinal": 0,
            "chapter": None,
            "page_start": 1,
            "page_end": 2,
        },
        {
            "chunk_id": "book-1:00001",
            "ordinal": 1,
            "chapter": "第1章 绪论",
            "page_start": 3,
            "page_end": 3,
        },
        {
            "chunk_id": "book-1:00002",
            "ordinal": 2,
            "chapter": "1.1 基本概念",
            "page_start": 4,
            "page_end": 5,
        },
        {
            "chunk_id": "book-1:00003",
            "ordinal": 3,
            "chapter": "1.1.1 泛化能力",
            "page_start": 6,
            "page_end": 6,
        },
    ]
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return path


def _setup_course(engine):
    imported = import_bundle(
        engine,
        _bundle(),
        student_id="student-1",
        course_title="测试课程",
    )
    bind_book(
        engine,
        student_id="student-1",
        source="test",
        source_course_id="course-1",
        book_id="book-1",
    )
    return imported


def test_outline_builds_hierarchy_links_chunks_and_is_idempotent(tmp_path) -> None:
    engine = _engine()
    _setup_course(engine)

    first = build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))
    second = build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    assert first.sections == 3
    assert first.linked_chunks == 3
    assert first.unassigned_chunks == 1
    assert first.duplicated is False
    assert second.duplicated is True
    with Session(engine) as session:
        sections = session.scalars(select(BookSection).order_by(BookSection.level)).all()
        assert [section.title for section in sections] == [
            "第1章 绪论",
            "1.1 基本概念",
            "1.1.1 泛化能力",
        ]
        assert sections[-1].parent_id == sections[1].id
        assert session.scalar(select(func.count()).select_from(BookSectionChunk)) == 3
        binding = session.scalar(select(CourseBook))
        assert binding is not None
        assert binding.book_version == first.index_version


def test_mapping_is_course_scoped_traceable_and_idempotent(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    seen_queries = []

    def fake_retrieve(query, book_id, top_k, threshold):
        seen_queries.append(query)
        assert book_id == "book-1"
        assert top_k == 12
        assert threshold == 0.65
        return [
            RetrievedChunk(
                chunk_id="book-1:00002",
                book_id="book-1",
                text="另一段向量距离更近但章节标题不匹配的内容。",
                chapter="1.1 基本概念",
                page_start=4,
                page_end=5,
                distance=0.1,
            ),
            RetrievedChunk(
                chunk_id="book-1:00003",
                book_id="book-1",
                text="泛化能力是模型适用于新样本的能力。",
                chapter="1.1.1 泛化能力",
                page_start=6,
                page_end=6,
                distance=0.2,
            ),
        ], None

    first = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("泛化能力")],
        retrieve=fake_retrieve,
    )
    second = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("泛化能力")],
        retrieve=fake_retrieve,
    )

    assert first.mappings[0].section_title == "1.1.1 泛化能力"
    assert first.mappings[0].confidence == 0.8
    assert len(first.mappings[0].lecture_evidence_ids) == 1
    assert first.mappings[0].book_evidence_ids[0] == "book:book-1:00003"
    assert second.mappings[0].mapping_id == first.mappings[0].mapping_id
    assert "泛化能力" in seen_queries
    assert any("模型的泛化能力" in query for query in seen_queries)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TopicMapping)) == 1


def test_mapping_uses_book_alias_without_replacing_original_topic(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    seen_queries = []

    def fake_retrieve(query, book_id, top_k, threshold):
        del book_id, top_k, threshold
        seen_queries.append(query)
        if query != "泛化能力":
            return [], "no match"
        return [
            RetrievedChunk(
                chunk_id="book-1:00003",
                book_id="book-1",
                text="泛化能力是模型适用于新样本的能力。",
                chapter="1.1.1 泛化能力",
                page_start=6,
                page_end=6,
                distance=0.2,
            )
        ], None

    result = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("过拟合后的表现", retrieval_aliases=("泛化能力", "  泛化能力 "))],
        retrieve=fake_retrieve,
    )

    assert "泛化能力" in seen_queries
    assert result.mappings[0].topic == "过拟合后的表现"
    assert result.mappings[0].section_title == "1.1.1 泛化能力"
    with Session(engine) as session:
        mapping = session.get(TopicMapping, result.mappings[0].mapping_id)
        assert mapping is not None
        assert mapping.topic == "过拟合后的表现"
        assert mapping.review_status == "pending"


def test_mapping_can_bind_an_explicit_subset_of_lecture_evidence(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))
    with Session(engine) as session:
        evidence = session.scalars(
            select(LectureSegment)
            .where(LectureSegment.lecture_id == imported.lecture_id)
            .order_by(LectureSegment.ordinal)
        ).all()

    def fake_retrieve(query, book_id, top_k, threshold):
        del query, book_id, top_k, threshold
        return [
            RetrievedChunk(
                chunk_id="book-1:00003",
                book_id="book-1",
                text="泛化能力是模型适用于新样本的能力。",
                chapter="1.1.1 泛化能力",
                page_start=6,
                page_end=6,
                distance=0.2,
            )
        ], None

    result = map_topics(
        engine,
        imported.lecture_id,
        [
            TopicInput(
                "课程通知对应的背景知识",
                lecture_evidence_ids=(f"lecture:{evidence[1].id}",),
            )
        ],
        retrieve=fake_retrieve,
    )

    assert result.mappings[0].lecture_evidence_ids == (f"lecture:{evidence[1].id}",)


def test_mapping_review_requires_traceable_mapping(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    def fake_retrieve(query, book_id, top_k, threshold):
        del query, book_id, top_k, threshold
        return [
            RetrievedChunk(
                chunk_id="book-1:00003",
                book_id="book-1",
                text="泛化能力是模型适用于新样本的能力。",
                chapter="1.1.1 泛化能力",
                page_start=6,
                page_end=6,
                distance=0.2,
            )
        ], None

    mapped = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("泛化能力")],
        retrieve=fake_retrieve,
    )
    reviewed = review_mapping(engine, mapped.mappings[0].mapping_id, "approved")

    assert reviewed.review_status == "approved"
    with Session(engine) as session:
        mapping = session.get(TopicMapping, reviewed.mapping_id)
        assert mapping is not None
        assert mapping.review_status == "approved"


def _two_section_retrieve():
    """向量排序偏好“1.1 基本概念”（0.9），略低于“1.1.1 泛化能力”（0.8）。"""

    def fake_retrieve(query, book_id, top_k, threshold):
        del query, book_id, top_k, threshold
        return [
            RetrievedChunk(
                chunk_id="book-1:00002",
                book_id="book-1",
                text="本节介绍基本概念。",
                chapter="1.1 基本概念",
                page_start=4,
                page_end=5,
                distance=0.1,
            ),
            RetrievedChunk(
                chunk_id="book-1:00003",
                book_id="book-1",
                text="泛化能力是模型适用于新样本的能力。",
                chapter="1.1.1 泛化能力",
                page_start=6,
                page_end=6,
                distance=0.2,
            ),
        ], None

    return fake_retrieve


def _fit_judge(fits_by_title, calls=None):
    def judge(state, questions):
        if calls is not None:
            calls.append((state, questions))
        assert set(questions) == {
            f"candidate_{index}" for index in range(len(state["candidates"]))
        }
        return {
            f"candidate_{index}": {
                "type": "score",
                "score": fits_by_title[candidate["section_title"]],
                "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0},
            }
            for index, candidate in enumerate(state["candidates"])
        }

    return judge


def test_mapping_judge_reorders_sections_and_changes_fingerprint(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    baseline = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
    )
    assert baseline.mappings[0].section_title == "1.1 基本概念"
    with Session(engine) as session:
        baseline_row = session.get(TopicMapping, baseline.mappings[0].mapping_id)
        assert baseline_row is not None
        baseline_fingerprint = baseline_row.input_fingerprint

    calls = []
    judged = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
        judge=_fit_judge({"1.1 基本概念": 1.0, "1.1.1 泛化能力": 2.0}, calls),
    )

    assert judged.mappings[0].section_title == "1.1.1 泛化能力"
    assert judged.mappings[0].confidence == 0.8
    assert judged.mappings[0].review_status == "pending"
    assert judged.mappings[0].mapping_id == baseline.mappings[0].mapping_id
    assert judged.mappings[0].book_evidence_ids[0] == "book:book-1:00003"
    assert len(calls) == 1
    state, questions = calls[0]
    assert state["topic"] == "课程通知对应的背景知识"
    assert [candidate["section_title"] for candidate in state["candidates"]] == [
        "1.1 基本概念",
        "1.1.1 泛化能力",
    ]
    assert questions["candidate_0"]["criteria"] == [
        "没有讲该主题",
        "相关但不是同一主题",
        "直接讲该主题",
    ]
    with Session(engine) as session:
        row = session.get(TopicMapping, judged.mappings[0].mapping_id)
        assert row is not None
        assert row.mapper_version == "section-map-v2"
        assert row.input_fingerprint != baseline_fingerprint
        assert session.scalar(select(func.count()).select_from(TopicMapping)) == 1


def test_mapping_judge_zero_fit_leaves_mapping_for_review(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    result = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
        judge=_fit_judge({"1.1 基本概念": 0.0, "1.1.1 泛化能力": 0.0}),
    )

    assert result.mappings[0].section_id is None
    assert result.mappings[0].section_title is None
    assert result.mappings[0].confidence is None
    assert result.mappings[0].book_evidence_ids == ()
    assert result.mappings[0].review_status == "needs_review"


def test_mapping_judge_malformed_answer_falls_back_to_vector(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    def broken_judge(state, questions):
        del state, questions
        return {"candidate_0": {"type": "score", "score": "high", "probabilities": {}}}

    result = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
        judge=broken_judge,
    )

    assert result.mappings[0].section_title == "1.1 基本概念"
    assert result.mappings[0].confidence == 0.9
    assert result.mappings[0].review_status == "pending"


def test_mapping_judge_does_not_touch_reviewed_mapping(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    baseline = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
    )
    review_mapping(engine, baseline.mappings[0].mapping_id, "approved")

    def unexpected_retrieve(*args):
        raise AssertionError("已审核映射不应重新检索或重排")

    calls = []
    result = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=unexpected_retrieve,
        judge=_fit_judge({"1.1 基本概念": 0.0, "1.1.1 泛化能力": 2.0}, calls),
    )

    assert result.mappings[0].section_title == "1.1 基本概念"
    assert result.mappings[0].review_status == "approved"
    assert result.mappings[0].mapping_id == baseline.mappings[0].mapping_id
    assert calls == []


def test_mapping_judge_ties_fall_back_to_vector_score(tmp_path) -> None:
    engine = _engine()
    imported = _setup_course(engine)
    build_book_outline(engine, "book-1", chunks_path=_chunks(tmp_path))

    result = map_topics(
        engine,
        imported.lecture_id,
        [TopicInput("课程通知对应的背景知识")],
        retrieve=_two_section_retrieve(),
        judge=_fit_judge({"1.1 基本概念": 2.0, "1.1.1 泛化能力": 2.0}),
    )

    assert result.mappings[0].section_title == "1.1 基本概念"
    assert result.mappings[0].confidence == 0.9

