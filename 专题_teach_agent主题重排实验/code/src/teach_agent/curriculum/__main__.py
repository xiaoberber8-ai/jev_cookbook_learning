"""P1 教材目录与课堂主题映射 CLI。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from teach_agent.config import (
    JEV_API_KEY,
    JEV_BASE_URL,
    JEV_MODEL,
    JEV_TIMEOUT,
    MAPPING_JUDGE,
)
from teach_agent.db import build_engine
from teach_agent.store import open_store

from .jev import make_jev_judge
from .mapping import TopicInput, map_topics, review_mapping
from .outline import build_book_outline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="teach_agent P1 课程—教材映射")
    commands = parser.add_subparsers(dest="command", required=True)

    outline = commands.add_parser("outline", help="从已索引 chunks 构建教材章节树")
    outline.add_argument("--book-id", required=True)

    mapper = commands.add_parser("map", help="把人工确认的课堂主题映射到教材小节")
    mapper.add_argument("--lecture-id", required=True)
    mapper.add_argument("--topic", action="append", required=True, help="可重复指定")
    mapper.add_argument(
        "--alias",
        action="append",
        default=[],
        help="教材中的同义词/别名；可重复指定，仅支持单个 --topic",
    )
    mapper.add_argument(
        "--evidence-id",
        action="append",
        default=[],
        help="明确绑定的课堂 evidence ID；可重复指定，仅支持单个 --topic",
    )
    mapper.add_argument(
        "--relation-type",
        choices=["direct", "prerequisite", "extension"],
        default="direct",
    )
    mapper.add_argument(
        "--judge",
        choices=["vector", "jev"],
        default=None,
        help="候选小节判定器；缺省读取 TEACH_MAPPING_JUDGE，默认 vector",
    )

    reviewer = commands.add_parser("review", help="人工确认或驳回一条主题映射")
    reviewer.add_argument("--mapping-id", required=True)
    reviewer.add_argument(
        "--status",
        required=True,
        choices=["pending", "approved", "rejected", "needs_review"],
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    engine = build_engine()
    try:
        if args.command == "outline":
            with open_store() as store:
                book = store.get(args.book_id)
            if book is None or book.status != "indexed":
                raise ValueError("book_id 不存在或教材尚未完成索引")
            result = build_book_outline(engine, args.book_id)
        elif args.command == "map":
            if (args.alias or args.evidence_id) and len(args.topic) != 1:
                raise ValueError(
                    "--alias/--evidence-id 与 --topic 对应，使用时每次只能映射一个主题"
                )
            topics = [
                TopicInput(
                    topic=topic,
                    relation_type=args.relation_type,
                    retrieval_aliases=tuple(args.alias),
                    lecture_evidence_ids=tuple(args.evidence_id),
                )
                for topic in args.topic
            ]
            judge_name = args.judge or MAPPING_JUDGE
            judge = None
            if judge_name == "jev":
                if not JEV_API_KEY:
                    raise ValueError("使用 Jev 判定需配置 TEACH_JEV_API_KEY 或 TYPESAFE_API_KEY")
                judge = make_jev_judge(
                    api_key=JEV_API_KEY,
                    model=JEV_MODEL,
                    base_url=JEV_BASE_URL,
                    timeout=JEV_TIMEOUT,
                )
            result = map_topics(engine, args.lecture_id, topics, judge=judge)
        else:
            result = review_mapping(engine, args.mapping_id, args.status)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        raise SystemExit(f"执行失败：{exc}") from exc
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
