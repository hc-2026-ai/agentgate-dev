from __future__ import annotations

import json

import pytest

from agentgate.domain import (
    Case,
    CaseTurn,
    EvaluatorKind,
    EvaluatorSpec,
    FailureStage,
    Outcome,
    Trace,
)
from agentgate.evaluator.executor import execute_evaluators
from agentgate.evaluator.judge.answer_quality import AnswerQualityJudge
from agentgate.evaluator.judge.contract import (
    MAX_REASON_CHARS,
    MAX_VIOLATION_DETAIL_CHARS,
    response_instructions,
)
from agentgate.evaluator.judge.model_protocol import (
    JudgeModelTimeout,
    JudgeRequest,
    JudgeResponse,
    request_fingerprint,
)


def verdict_json(
    verdict: str = "pass",
    score: float = 1.0,
    confidence: float = 1.0,
    reason: str = "looks right",
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "score": score,
            "confidence": confidence,
            "reason": reason,
            "violations": (
                []
                if verdict == "pass"
                else [{"criterion": "correctness", "detail": "answer is incorrect"}]
            ),
        }
    )


class RecordingModel:
    provider_id = "test-provider"

    def __init__(
        self,
        response: JudgeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or JudgeResponse(
            text=verdict_json(),
            resolved_model_id="resolved-model",
            request_id="request-1",
            input_tokens=20,
            output_tokens=10,
            latency_ms=12.5,
            finish_reason="stop",
        )
        self.error = error
        self.requests: list[JudgeRequest] = []

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def judge_spec(**overrides: object) -> EvaluatorSpec:
    config: dict[str, object] = {
        "model": {
            "provider_id": "test-provider",
            "model_id": "requested-model",
            "credential_ref": "env:JUDGE_API_KEY",
        },
        "instruction": "Judge answer quality.",
        "rubric": {"criteria": ["correct", "complete"]},
    }
    config.update(overrides)
    return EvaluatorSpec(
        id="answer-quality",
        name="Answer quality",
        kind=EvaluatorKind.LLM_JUDGE,
        dimension="answer",
        metric="answer_quality",
        implementation_id="answer_quality",
        config=config,
    )


def evaluation_case() -> Case:
    return Case(
        id="case",
        name="case",
        turns=(
            CaseTurn(id="turn-1", input={"message": "hello"}),
            CaseTurn(id="turn-2", input={"message": "continue"}),
        ),
    )


def evaluation_trace(final_output: dict | None = None) -> Trace:
    return Trace(
        trace_id="0" * 32,
        run_id="run",
        case_id="case",
        spans=(),
        final_output={"answer": "approved"} if final_output is None else final_output,
    )


def execute(
    model: RecordingModel,
    *,
    spec: EvaluatorSpec | None = None,
    trace: Trace | None = None,
):
    return execute_evaluators(
        evaluation_case(),
        trace or evaluation_trace(),
        (spec or judge_spec(),),
        {("answer_quality", "1"): AnswerQualityJudge({"test-provider": model})},
    )[0]


def test_validate_spec_accepts_valid_configuration_without_model_call() -> None:
    model = RecordingModel()
    judge = AnswerQualityJudge({"test-provider": model})

    judge.validate_spec(judge_spec())

    assert model.requests == []


@pytest.mark.parametrize(
    "config_override",
    [
        {"unexpected": True},
        {"instruction": " "},
        {"rubric": {}},
        {"instruction": "Judge {{unknown}}", "render_template": True},
        {"user_prompt": "Judge {{reference}}", "render_template": True},
        {"user_prompt": " "},
        {"user_prompt": 123},
        {"render_template": 1},
        {"render_template": "true"},
        {"render_template": None},
    ],
)
def test_validate_spec_rejects_invalid_configuration(
    config_override: dict[str, object],
) -> None:
    judge = AnswerQualityJudge({"test-provider": RecordingModel()})

    with pytest.raises(ValueError):
        judge.validate_spec(judge_spec(**config_override))


def test_validate_spec_rejects_unknown_provider() -> None:
    judge = AnswerQualityJudge({"test-provider": RecordingModel()})
    spec = judge_spec(model={"provider_id": "missing", "model_id": "model"})

    with pytest.raises(ValueError, match="no Judge model client configured"):
        judge.validate_spec(spec)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": EvaluatorKind.RULE}, "requires kind"),
        ({"implementation_id": "other"}, "requires implementation_id"),
        ({"implementation_version": "2"}, "requires implementation_version"),
    ],
)
def test_validate_spec_rejects_wrong_implementation_identity(
    changes: dict[str, object],
    message: str,
) -> None:
    judge = AnswerQualityJudge({"test-provider": RecordingModel()})

    with pytest.raises(ValueError, match=message):
        judge.validate_spec(judge_spec().model_copy(update=changes))


def test_pass_verdict_maps_to_result_and_records_provenance() -> None:
    model = RecordingModel()

    result = execute(model)

    assert result.outcome == Outcome.PASS
    assert result.score == 1.0
    assert result.checks[0].turn_id is None
    assert result.checks[0].methods[0].implementation_id == "answer_quality"
    assert result.judge_record.provider_id == "test-provider"
    assert result.judge_record.request_system_prompt == model.requests[0].system_prompt
    assert result.judge_record.request_user_prompt == model.requests[0].user_prompt
    assert result.judge_record.requested_model == "requested-model"
    assert result.judge_record.resolved_model == "resolved-model"
    assert result.judge_record.request_sha256 == request_fingerprint(model.requests[0])
    assert result.judge_record.request_id == "request-1"
    assert result.judge_record.input_tokens == 20
    assert result.judge_record.output_tokens == 10
    assert result.judge_record.latency_ms == 12.5
    assert len(model.requests) == 1


def test_prompt_configuration_executes_and_records_rendered_evidence() -> None:
    model = RecordingModel()
    spec = judge_spec(
        instruction="Evaluate conciseness.\n<input>{{input}}</input>\n<output>{{output}}</output>",
        user_prompt="Give a score and reason for {{output}}.",
        render_template=True,
    )

    result = execute(model, spec=spec)

    assert result.outcome == Outcome.PASS
    assert result.score == 1.0
    assert result.checks[0].reason == "looks right"
    assert "hello" in model.requests[0].system_prompt
    assert "continue" in model.requests[0].system_prompt
    assert '<output>{"answer":"approved"}</output>' in model.requests[0].system_prompt
    assert model.requests[0].user_prompt.startswith('Give a score and reason for {"answer":"approved"}.')
    assert result.judge_record.request_system_prompt == model.requests[0].system_prompt
    assert result.judge_record.request_user_prompt == model.requests[0].user_prompt
    assert spec.config["instruction"].endswith("<output>{{output}}</output>")


def test_historical_prompt_literals_remain_executable_without_template_switch() -> None:
    model = RecordingModel()
    spec = judge_spec(instruction="Explain the literal {{reference}} and {{input}}.")

    result = execute(model, spec=spec)

    assert result.outcome == Outcome.PASS
    assert model.requests[0].system_prompt.startswith("Explain the literal {{reference}} and {{input}}.")


@pytest.mark.parametrize(
    ("verdict", "score", "expected"),
    [
        ("fail", 0.2, Outcome.FAIL),
        ("review", 0.8, Outcome.REVIEW),
    ],
)
def test_maps_nonpassing_verdicts(
    verdict: str,
    score: float,
    expected: Outcome,
) -> None:
    model = RecordingModel(
        JudgeResponse(
            text=verdict_json(verdict, score, reason="model reason"),
            resolved_model_id="resolved-model",
        )
    )

    result = execute(model)

    assert result.outcome == expected
    assert result.checks[0].reason == "model reason"
    if expected == Outcome.FAIL:
        assert result.primary_failure_stage == FailureStage.FINAL_OUTPUT
        assert result.checks[0].failure_sequence == 0
    else:
        assert result.primary_failure_stage is None


def test_low_confidence_escalates_to_review() -> None:
    model = RecordingModel(
        JudgeResponse(
            text=verdict_json("pass", 0.9, confidence=0.4),
            resolved_model_id="resolved-model",
        )
    )

    result = execute(model, spec=judge_spec(min_confidence=0.7))

    assert result.outcome == Outcome.REVIEW
    assert "below min_confidence" in result.checks[0].reason


def test_empty_final_output_is_not_applicable_without_model_call() -> None:
    model = RecordingModel()

    result = execute(model, trace=evaluation_trace({}))

    assert result.outcome == Outcome.NOT_APPLICABLE
    assert result.score is None
    assert result.judge_record is None
    assert model.requests == []


def test_request_uses_defaults_and_redacts_all_selected_material() -> None:
    model = RecordingModel()
    sensitive_case = Case(
        id="case",
        name="case",
        turns=(CaseTurn(id="turn-1", input={"email": "alice@example.com"}),),
    )
    sensitive_trace = evaluation_trace(
        {"message": "authorization=top-secret"}
    )
    spec = judge_spec(
        input_selection="full_trajectory",
        rubric={"customer_email": "reviewer@example.com"},
    )

    result = execute_evaluators(
        sensitive_case,
        sensitive_trace,
        (spec,),
        {("answer_quality", "1"): AnswerQualityJudge({"test-provider": model})},
    )[0]

    assert result.outcome == Outcome.PASS
    assert len(model.requests) == 1
    request = model.requests[0]
    sent = f"{request.system_prompt}\n{request.user_prompt}"
    assert "alice@example.com" not in sent
    assert "reviewer@example.com" not in sent
    assert "top-secret" not in sent
    assert "[redacted]" in sent
    assert request.temperature == 0.0
    assert request.max_output_tokens == 6000
    assert request.timeout_seconds == 60.0


@pytest.mark.parametrize(
    "config_override",
    [
        {"unexpected": True},
        {"instruction": " "},
        {"rubric": {}},
        {"pass_threshold": 2},
        {"min_confidence": -1},
        {"input_selection": "everything"},
        {"max_output_tokens": 0},
    ],
)
def test_invalid_configuration_becomes_error_result(
    config_override: dict[str, object],
) -> None:
    model = RecordingModel()

    result = execute(model, spec=judge_spec(**config_override))

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert model.requests == []


def test_missing_provider_becomes_error_result() -> None:
    model = RecordingModel()
    spec = judge_spec(
        model={"provider_id": "missing", "model_id": "requested-model"}
    )

    result = execute(model, spec=spec)

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert model.requests == []


@pytest.mark.parametrize(
    ("model", "category", "retryable"),
    [
        (
            RecordingModel(
                JudgeResponse(
                    text="not json",
                    resolved_model_id="resolved-model",
                )
            ),
            "invalid_output",
            False,
        ),
        (
            RecordingModel(error=JudgeModelTimeout("slow")),
            "timeout",
            True,
        ),
        (
            RecordingModel(
                JudgeResponse(
                    text=verdict_json(),
                    resolved_model_id="resolved-model",
                    finish_reason="length",
                )
            ),
            "invalid_output",
            False,
        ),
    ],
)
def test_model_failures_become_isolated_error_results(
    model: RecordingModel,
    category: str,
    retryable: bool,
) -> None:
    result = execute(model)

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == category
    assert result.error_detail.retryable is retryable


def test_rejects_mismatched_client_provider_identity() -> None:
    model = RecordingModel()

    with pytest.raises(ValueError, match="does not match provider_id"):
        AnswerQualityJudge({"different": model})


INVALID_CONTRACT_RESPONSES = [
    pytest.param(verdict_json(score=90), "'score' must be between 0 and 1", id="score-range"),
    pytest.param("not JSON", "not valid JSON", id="invalid-json"),
    pytest.param(
        json.dumps({**json.loads(verdict_json()), "review": True}),
        "unknown fields: review", id="extra-review-field",
    ),
    pytest.param(
        verdict_json(reason="证" * (MAX_REASON_CHARS + 1)),
        f"'reason' exceeds {MAX_REASON_CHARS} characters", id="reason-too-long",
    ),
    pytest.param(
        json.dumps({
            **json.loads(verdict_json()),
            "violations": [{"criterion": "准确性", "detail": "x" * (MAX_VIOLATION_DETAIL_CHARS + 1)}],
        }),
        f"'violations[0].detail' exceeds {MAX_VIOLATION_DETAIL_CHARS} characters",
        id="detail-too-long",
    ),
]


@pytest.mark.parametrize("invalid_text,error_fragment", INVALID_CONTRACT_RESPONSES)
def test_contract_correction_is_bounded_and_audited(invalid_text, error_fragment) -> None:
    class CorrectingModel(RecordingModel):
        def complete(self, request):
            self.requests.append(request)
            return JudgeResponse(text=invalid_text if len(self.requests) == 1
                                 else verdict_json(verdict="fail", score=0.2),
                                 resolved_model_id="resolved-model")

    model = CorrectingModel()
    result = execute(model, spec=judge_spec(pass_threshold=0.85))
    assert result.outcome == Outcome.FAIL
    assert len(model.requests) == 2
    assert model.requests[1].timeout_seconds <= model.requests[0].timeout_seconds
    first, correction = model.requests
    assert correction.system_prompt.startswith(first.system_prompt)
    assert first.user_prompt == correction.user_prompt
    for request in model.requests:
        assert "Judge answer quality." in request.system_prompt
        assert response_instructions(0.85) in request.system_prompt
    assert "上次响应未通过输出校验" in correction.system_prompt
    assert error_fragment in correction.system_prompt
    assert result.judge_record.request_sha256 == request_fingerprint(model.requests[1])
    assert len(result.judge_record.previous_attempts) == 1
    assert result.judge_record.previous_attempts[0].raw_response == invalid_text
    assert result.judge_record.previous_attempts[0].request_sha256 == request_fingerprint(model.requests[0])


@pytest.mark.parametrize("invalid_text,error_fragment", INVALID_CONTRACT_RESPONSES)
def test_repeated_invalid_contract_remains_error_with_both_responses(
    invalid_text, error_fragment,
) -> None:
    model = RecordingModel(JudgeResponse(text=invalid_text,
                                        resolved_model_id="resolved-model"))
    result = execute(model)
    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert error_fragment in result.error_detail.message
    assert len(model.requests) == 2
    assert result.score is None
    assert len(result.judge_record.previous_attempts) == 1
    assert result.judge_record.raw_response == invalid_text
    assert result.judge_record.previous_attempts[0].raw_response == invalid_text


def test_annotation_evidence_prefers_actual_request_and_keeps_full_response():
    from agentgate.application.annotation_evidence import evaluator_annotation_evidence
    model = RecordingModel()
    result = execute(model)
    payload = evaluator_annotation_evidence(judge_spec(), evaluation_case(), evaluation_trace(), result)
    assert payload['prompt_source'] == 'recorded'
    assert payload['system_prompt'] == model.requests[0].system_prompt
    assert payload['user_prompt'] == model.requests[0].user_prompt
    assert payload['result']['judge_record']['raw_response'] == model.response.text
    assert payload['evidence']['case']['id'] == 'case'
    assert len(model.requests) == 1


def test_annotation_evidence_labels_reconstructed_historical_request():
    from agentgate.application.annotation_evidence import evaluator_annotation_evidence
    result = execute(RecordingModel())
    old = result.model_copy(update={'judge_record': result.judge_record.model_copy(update={
        'request_system_prompt': None, 'request_user_prompt': None})})
    payload = evaluator_annotation_evidence(judge_spec(), evaluation_case(), evaluation_trace(), old)
    assert payload['prompt_source'] == 'reconstructed'
    assert payload['request_hash_matches'] is True
    changed = old.model_copy(update={'judge_record': old.judge_record.model_copy(update={'request_sha256': 'a' * 64})})
    assert evaluator_annotation_evidence(judge_spec(), evaluation_case(), evaluation_trace(), changed)['request_hash_matches'] is False


def test_annotation_rule_evidence_exposes_versioned_code_without_execution():
    from agentgate.application.annotation_evidence import evaluator_annotation_evidence
    spec = EvaluatorSpec(id='output', name='Output', dimension='output', metric='match', implementation_id='final_output')
    payload = evaluator_annotation_evidence(spec, evaluation_case(), evaluation_trace(), None)
    assert 'class FinalOutputEvaluator' in payload['code'][0]['source']
    assert payload['result'] is None
    unknown = spec.model_copy(update={'implementation_version': 'unknown'})
    assert not evaluator_annotation_evidence(unknown, evaluation_case(), evaluation_trace(), None)['code']
