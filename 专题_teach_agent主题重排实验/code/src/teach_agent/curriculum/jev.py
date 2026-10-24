"""Jev（typesafe.ai）类型化判断的最小客户端。

只覆盖 P1 主题重排需要的能力：一次 ``POST /v1/systemone`` 提交若干
choice/score/noul 问题，返回服务端的 ``answers``。任何网络、协议或结构
异常都返回 ``None``，由调用方回落到本地向量排序，避免把外部服务故障
升级成映射失败。
"""

from __future__ import annotations

import http.client
import json
import urllib.request
from collections.abc import Callable
from typing import Any

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 8.0

Answers = dict[str, dict[str, Any]]
# 判定器契约：输入 (state, questions)，返回 answers；失败返回 None。
Judge = Callable[[dict[str, Any], dict[str, Any]], Answers | None]


def _valid_answer(question: dict[str, Any], answer: object) -> bool:
    if not isinstance(answer, dict):
        return False
    if not isinstance(answer.get("probabilities"), dict):
        return False
    question_type = question.get("type")
    value = answer.get(question_type)
    if question_type == "choice":
        return isinstance(value, str) and value != ""
    if question_type in {"score", "noul"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def request_systemone(
    state: dict[str, Any],
    questions: dict[str, Any],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> Answers | None:
    """提交一次 System One 判定，返回经过最小结构校验的 answers。"""
    if not api_key.strip() or not questions:
        return None
    payload = json.dumps(
        {"state": state, "model": model, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/systemone",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None

    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        return None
    for name, question in questions.items():
        if not isinstance(question, dict) or not _valid_answer(question, answers.get(name)):
            return None
    return answers


def make_jev_judge(
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> Judge:
    """构造用于依赖注入的 Jev 判定器；密钥缺失时每次判定都回落。"""

    def judge(state: dict[str, Any], questions: dict[str, Any]) -> Answers | None:
        return request_systemone(
            state,
            questions,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )

    return judge
