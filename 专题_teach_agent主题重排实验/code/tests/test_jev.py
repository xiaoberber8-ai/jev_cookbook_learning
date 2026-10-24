from __future__ import annotations

import json
import urllib.request

from teach_agent.curriculum.jev import request_systemone

QUESTIONS = {
    "candidate_0": {
        "type": "score",
        "instructions": "匹配程度？",
        "criteria": ["没有讲该主题", "相关但不是同一主题", "直接讲该主题"],
    }
}


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _answers(score: object) -> bytes:
    return json.dumps(
        {
            "answers": {
                "candidate_0": {
                    "type": "score",
                    "score": score,
                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                    "legend": {"0": "没有讲该主题", "1": "相关但不是同一主题", "2": "直接讲该主题"},
                }
            }
        }
    ).encode()


def test_request_systemone_sends_contract_and_returns_answers(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(_answers(1.99))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    answers = request_systemone(
        {"topic": "泛化能力"},
        QUESTIONS,
        api_key="test-key",
        model="jev-latest",
        base_url="https://example.test/",
        timeout=3.0,
    )

    assert answers is not None
    assert answers["candidate_0"]["score"] == 1.99
    request = captured["request"]
    assert request.full_url == "https://example.test/v1/systemone"
    assert request.get_header("Authorization") == "Bearer test-key"
    payload = json.loads(request.data)
    assert payload["model"] == "jev-latest"
    assert payload["state"] == {"topic": "泛化能力"}
    assert payload["questions"] == QUESTIONS
    assert captured["timeout"] == 3.0


def test_request_systemone_returns_none_on_failure_or_bad_schema(monkeypatch) -> None:
    def broken_urlopen(request, timeout):
        del request, timeout
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", broken_urlopen)
    assert request_systemone({}, QUESTIONS, api_key="test-key") is None
    assert request_systemone({}, QUESTIONS, api_key="") is None

    def missing_probabilities(request, timeout):
        del request, timeout
        return _FakeResponse(
            json.dumps({"answers": {"candidate_0": {"type": "score", "score": 1.0}}}).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", missing_probabilities)
    assert request_systemone({}, QUESTIONS, api_key="test-key") is None
