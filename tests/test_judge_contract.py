from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from agentgate.evaluator.judge.contract import (
    MAX_CRITERION_CHARS,
    MAX_REASON_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_VIOLATION_DETAIL_CHARS,
    MAX_VIOLATIONS,
    JudgeContractError,
    parse_verdict,
    response_instructions,
)
from agentgate.evaluator.judge.model_protocol import (
    JudgeModelClient,
    JudgeModelInvalidResponse,
    JudgeModelTimeout,
    JudgeRequest,
    JudgeResponse,
    request_fingerprint,
)


def test_judge_request_is_secret_free_immutable_and_stably_fingerprinted() -> None:
    request = JudgeRequest(
        model_id="judge-1",
        system_prompt="Apply the rubric.",
        user_prompt="Evaluate this output.",
        seed=7,
        max_output_tokens=256,
    )

    assert request_fingerprint(request) == request_fingerprint(request)
    assert request_fingerprint(
        JudgeRequest(model_id="judge-1", user_prompt="Other output.")
    ) != request_fingerprint(request)
    assert "credential" not in request.__dataclass_fields__
    with pytest.raises(FrozenInstanceError):
        request.model_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_id": " "}, "model_id"),
        ({"user_prompt": ""}, "user_prompt"),
        ({"temperature": 2.1}, "temperature"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"response_format": "xml"}, "response_format"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
    ],
)
def test_judge_request_rejects_invalid_fields(changes, message) -> None:
    values = {"model_id": "judge-1", "user_prompt": "Evaluate."}
    with pytest.raises(ValueError, match=message):
        JudgeRequest(**{**values, **changes})


def test_request_fingerprint_excludes_only_the_timeout() -> None:
    baseline = JudgeRequest(model_id="judge-1", user_prompt="Evaluate.")
    longer_wait = JudgeRequest(
        model_id="judge-1",
        user_prompt="Evaluate.",
        timeout_seconds=120,
    )
    changed_generation = JudgeRequest(
        model_id="judge-1",
        user_prompt="Evaluate.",
        temperature=0.5,
    )

    assert request_fingerprint(longer_wait) == request_fingerprint(baseline)
    assert request_fingerprint(changed_generation) != request_fingerprint(baseline)


def test_judge_response_validates_accounting_and_reports_truncation() -> None:
    response = JudgeResponse(
        text='{"verdict":"pass"}',
        resolved_model_id="judge-1-2026",
        input_tokens=10,
        output_tokens=4,
        latency_ms=12.5,
        finish_reason="length",
        attempt_count=2,
    )

    assert response.truncated is True
    with pytest.raises(ValueError, match="input_tokens"):
        JudgeResponse(text="{}", resolved_model_id="judge-1", input_tokens=-1)
    with pytest.raises(ValueError, match="attempt_count"):
        JudgeResponse(text="{}", resolved_model_id="judge-1", attempt_count=0)


def test_judge_client_protocol_and_error_classification() -> None:
    class Client:
        provider_id = "fake"

        def complete(self, request: JudgeRequest) -> JudgeResponse:
            return JudgeResponse(text="{}", resolved_model_id=request.model_id)

    assert isinstance(Client(), JudgeModelClient)
    assert isinstance(JudgeModelTimeout("slow"), TimeoutError)
    assert isinstance(JudgeModelInvalidResponse("bad envelope"), ValueError)


def verdict_json(**changes) -> str:
    payload = {
        "verdict": "pass",
        "score": 1.0,
        "confidence": 0.9,
        "reason": "The answer satisfies the rubric.",
        "violations": [],
    }
    payload.update(changes)
    return json.dumps(payload)


def test_parse_verdict_returns_immutable_normalized_content() -> None:
    parsed = parse_verdict(
        verdict_json(
            verdict="fail",
            score=0.25,
            violations=[{"criterion": "accuracy", "detail": "Amount is wrong."}],
        ),
        pass_threshold=0.8,
    )

    assert parsed.verdict == "fail"
    assert parsed.score == 0.25
    assert parsed.violations[0].criterion == "accuracy"
    with pytest.raises(FrozenInstanceError):
        parsed.score = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        "[]",
        verdict_json(verdict="unknown"),
        verdict_json(verdict="通过"),
        verdict_json(verdict="PASS"),
        verdict_json(verdict=None),
        verdict_json(score=True),
        verdict_json(score="0.8"),
        verdict_json(score=None),
        verdict_json(confidence=2),
        verdict_json(confidence="0.9"),
        verdict_json(confidence=None),
        verdict_json(reason=" "),
        verdict_json(reason=None),
        verdict_json(violations={}),
        verdict_json(violations=None),
        verdict_json(violations=["违规说明"]),
        verdict_json(violations=[None]),
        verdict_json(violations=[{"criterion": None, "detail": "理由"}]),
        verdict_json(violations=[{"criterion": "准确性", "detail": None}]),
    ],
)
def test_parse_verdict_rejects_malformed_content(text) -> None:
    with pytest.raises(JudgeContractError):
        parse_verdict(text, pass_threshold=0.8)


@pytest.mark.parametrize("field", ["verdict", "score", "confidence", "reason", "violations"])
def test_parse_verdict_requires_every_field(field) -> None:
    missing = json.loads(verdict_json())
    missing.pop(field)

    with pytest.raises(JudgeContractError, match=f"missing fields: {field}"):
        parse_verdict(json.dumps(missing), 0.8)


def test_parse_verdict_rejects_unknown_fields() -> None:
    unknown = json.loads(verdict_json())
    unknown["commentary"] = "extra"

    with pytest.raises(JudgeContractError, match="unknown fields: commentary"):
        parse_verdict(json.dumps(unknown), 0.8)


@pytest.mark.parametrize("verdict,score", [("pass", 1), ("fail", 0.2), ("review", 0.5)])
def test_chinese_explanations_preserve_english_enums_and_evidence_identifiers(verdict, score):
    parsed = parse_verdict(verdict_json(
        verdict=verdict, score=score, reason="根据 lookup_account 的结果判断。",
        violations=[{"criterion": "准确性", "detail": "应保留 account_id 原值。"}],
    ), 0.8)

    assert parsed.verdict == verdict
    assert parsed.score == score
    assert parsed.violations[0].criterion == "准确性"
    assert "lookup_account" in parsed.reason
    assert "account_id" in parsed.violations[0].detail


def test_parse_verdict_rejects_score_and_verdict_contradictions() -> None:
    with pytest.raises(JudgeContractError, match="pass verdict"):
        parse_verdict(verdict_json(verdict="pass", score=0.79), 0.8)
    with pytest.raises(JudgeContractError, match="fail verdict"):
        parse_verdict(verdict_json(verdict="fail", score=0.8), 0.8)

    assert parse_verdict(
        verdict_json(verdict="review", score=0.95), 0.8
    ).verdict == "review"


def test_parse_verdict_enforces_all_output_bounds() -> None:
    with pytest.raises(JudgeContractError, match="response exceeds"):
        parse_verdict("x" * (MAX_RESPONSE_CHARS + 1), 0.8)
    with pytest.raises(JudgeContractError, match="reason.*exceeds"):
        parse_verdict(verdict_json(reason="x" * (MAX_REASON_CHARS + 1)), 0.8)
    with pytest.raises(JudgeContractError, match="more than"):
        parse_verdict(
            verdict_json(
                violations=[{"criterion": "c", "detail": "d"}]
                * (MAX_VIOLATIONS + 1)
            ),
            0.8,
        )
    with pytest.raises(JudgeContractError, match="criterion.*exceeds"):
        parse_verdict(
            verdict_json(
                violations=[
                    {"criterion": "x" * (MAX_CRITERION_CHARS + 1), "detail": "d"}
                ]
            ),
            0.8,
        )


def test_suggested_output_budgets_do_not_reduce_existing_contract_limits() -> None:
    text = verdict_json(
        verdict="fail", score=0.2,
        reason="证" * MAX_REASON_CHARS,
        violations=[
            {"criterion": "项" * MAX_CRITERION_CHARS,
             "detail": "据" * MAX_VIOLATION_DETAIL_CHARS}
            for _ in range(MAX_VIOLATIONS // 2 + 1)
        ],
    )
    assert MAX_RESPONSE_CHARS // 2 < len(text) < MAX_RESPONSE_CHARS

    parsed = parse_verdict(text, pass_threshold=0.8)

    assert parsed.verdict == "fail"
    assert len(parsed.violations) == MAX_VIOLATIONS // 2 + 1
    assert len(parsed.violations[0].criterion) == MAX_CRITERION_CHARS
    assert len(parsed.violations[0].detail) == MAX_VIOLATION_DETAIL_CHARS


@pytest.mark.parametrize("threshold", [0, 0.85, 1])
def test_response_instructions_include_valid_example_bounds_and_threshold(threshold) -> None:
    instructions = response_instructions(threshold)

    assert "【平台输出协议】" in instructions
    assert "顶层必须且只能包含" in instructions
    assert '"verdict"' in instructions
    assert "pass|fail|review" in instructions
    assert f"本次通过阈值为 {threshold:g}" in instructions
    for bound in (
        MAX_REASON_CHARS, MAX_VIOLATIONS, MAX_CRITERION_CHARS,
        MAX_VIOLATION_DETAIL_CHARS, MAX_RESPONSE_CHARS,
    ):
        assert str(bound) in instructions
    assert "不按英文单词数或 token 数计算" in instructions
    assert "reason 必须为非空字符串，必须使用简体中文" in instructions
    assert "detail 必须为非空字符串，必须使用简体中文" in instructions
    assert "criterion 必须为非空字符串，必须使用简体中文" in instructions
    assert "工具名、代码、标识及必要的原文引用保留原样" in instructions
    assert "JSON 字段名和固定枚举值保持英文，不得翻译" in instructions
    assert f"建议不超过 {MAX_REASON_CHARS // 2} 字符" in instructions
    assert f"建议不超过 {MAX_VIOLATION_DETAIL_CHARS // 2} 字符" in instructions
    assert f"建议不超过 {MAX_CRITERION_CHARS // 2} 字符" in instructions
    assert f"建议不超过 {MAX_VIOLATIONS // 2} 条" in instructions
    assert f"完整响应文本建议不超过 {MAX_RESPONSE_CHARS // 2} 字符" in instructions
    assert "不得为了满足建议条数而省略独立违规" in instructions
    example = next(line for line in instructions.splitlines() if line.startswith("{"))
    parsed = parse_verdict(example, threshold)
    assert parsed.reason
    with pytest.raises(ValueError, match="pass_threshold"):
        response_instructions(1.1)
