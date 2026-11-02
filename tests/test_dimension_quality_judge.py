from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentgate.application import RunManagement, TargetCatalog
from agentgate.application.annotation_evidence import evaluator_annotation_evidence
from agentgate.application.evaluator_management import build_default_evaluator_management
from agentgate.demo.bootstrap import ensure_demo_dataset, ensure_demo_target_descriptors
from agentgate.demo.loan import LOAN_DATASET
from agentgate.demo.targets import build_demo_target_snapshot, get_demo_target_descriptor
from agentgate.domain import (
    Case,
    CaseTurn,
    EvaluatorKind,
    EvaluatorRef,
    EvaluatorSpec,
    Outcome,
    Trace,
)
from agentgate.evaluator.executor import execute_evaluators
from agentgate.evaluator.judge.contract import MAX_REASON_CHARS
from agentgate.evaluator.judge.dimension_contract import dimension_response_instructions
from agentgate.evaluator.judge.dimension_quality import (
    DimensionQualityJudge,
    build_dimension_quality_request,
)
from agentgate.evaluator.judge.model_protocol import (
    JudgeModelTimeout,
    JudgeResponse,
    request_fingerprint,
)
from agentgate.server.dependencies import get_dependencies
from agentgate.server.routes.evaluators import router
from agentgate.storage.sqlite import SQLiteRepository


def config() -> dict:
    return {
        "model": {"provider_id": "test", "model_id": "judge", "credential_ref": "env:JUDGE_KEY"},
        "dimensions": [
            {
                "id": "correct",
                "name": "正确性",
                "description": "事实和证据",
                "prompt": "检查事实",
                "weight": 80,
            },
            {
                "id": "concise",
                "name": "简洁性",
                "description": "",
                "prompt": "检查简洁",
                "weight": 20,
            },
        ],
        "input_selection": "final_output",
        "pass_threshold": 0.8,
    }


def spec(configuration: dict | None = None) -> EvaluatorSpec:
    return EvaluatorSpec(
        id="dimensions",
        name="维度评估",
        kind=EvaluatorKind.LLM_JUDGE,
        dimension="answer",
        metric="answer_quality",
        implementation_id="answer_quality",
        implementation_version="2",
        config=configuration or config(),
    )


def case() -> Case:
    return Case(id="case", name="Case", turns=(CaseTurn(id="turn", input={"message": "hello"}),))


def trace() -> Trace:
    return Trace(
        trace_id="a" * 32, run_id="run", case_id="case", spans=(), final_output={"answer": "ok"}
    )


def response_data(score: float = 1) -> dict:
    return {
        "dimensions": [
            {"id": "concise", "score": 0, "confidence": 0.9, "reason": "Too long", "review": False},
            {
                "id": "correct",
                "score": score,
                "confidence": 1,
                "reason": "Correct facts",
                "review": False,
            },
        ]
    }


class RecordingModel:
    provider_id = "test"

    def __init__(self, *responses):
        self.responses = list(responses) or [response_data()]
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, JudgeResponse):
            return response
        return JudgeResponse(
            text=json.dumps(response),
            resolved_model_id="resolved-judge",
            request_id="request",
            input_tokens=20,
            output_tokens=15,
            latency_ms=1,
        )


def execute(model, configuration=None, execution_trace=None):
    return execute_evaluators(
        case(),
        execution_trace or trace(),
        (spec(configuration),),
        {("answer_quality", "2"): DimensionQualityJudge({"test": model})},
    )[0]


def test_weighted_score_is_computed_by_code_with_exact_threshold_and_audit():
    model = RecordingModel()
    result = execute(model)
    assert result.score == 0.8
    assert result.outcome == Outcome.PASS
    actual = result.checks[0].actual
    assert [item["id"] for item in actual["dimensions"]] == ["correct", "concise"]
    assert actual["dimensions"][0]["weight"] == 80
    assert actual["dimensions"][0]["name"] == "正确性"
    assert actual["confidence"] == 0.9
    assert result.checks[0].methods[0].implementation_version == "2"
    assert result.judge_record.request_system_prompt == model.requests[0].system_prompt
    assert result.judge_record.request_user_prompt == model.requests[0].user_prompt
    assert result.judge_record.request_sha256 == request_fingerprint(model.requests[0])
    assert result.judge_record.resolved_model == "resolved-judge"
    assert json.loads(result.judge_record.raw_response) == response_data()
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "scores,weights,expected",
    [
        ((1, 0), (25, 75), 0.25),
        ((0.8, 0.8), (35, 65), 0.8),
        ((1, 1), (33.3, 66.7), 1),
    ],
)
def test_weight_changes_deterministically_affect_score(scores, weights, expected):
    configuration = config()
    payload = response_data()
    payload["dimensions"][1]["score"], payload["dimensions"][0]["score"] = scores
    for dimension, weight in zip(configuration["dimensions"], weights, strict=True):
        dimension["weight"] = weight
    result = execute(RecordingModel(payload), configuration)
    assert result.score == expected
    assert result.outcome == (Outcome.PASS if expected >= 0.8 else Outcome.FAIL)


@pytest.mark.parametrize(
    "change",
    [
        {"dimensions": []},
        {"extra": 1},
        {"pass_threshold": 80},
        {"temperature": True},
        {"timeout_seconds": 0},
        {"max_output_tokens": True},
        {"input_selection": "unknown"},
        {"model": {"provider_id": " ", "model_id": "model"}},
        {"model": {"provider_id": "other", "model_id": "model"}},
    ],
)
def test_invalid_configuration_rejected_before_model_call(change):
    configuration = config()
    configuration.update(change)
    model = RecordingModel()
    with pytest.raises(ValueError):
        DimensionQualityJudge({"test": model}).validate_spec(spec(configuration))
    assert model.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("weight", 0),
        ("weight", -1),
        ("weight", True),
        ("weight", 81),
        ("id", " "),
        ("id", "concise"),
        ("name", ""),
        ("prompt", " "),
        ("extra", "x"),
    ],
)
def test_dimension_invariants_rejected(field, value):
    configuration = config()
    configuration["dimensions"][0][field] = value
    with pytest.raises(ValueError):
        DimensionQualityJudge({"test": RecordingModel()}).validate_spec(spec(configuration))


@pytest.mark.parametrize("review,min_confidence", [(True, 0), (False, 0.95)])
def test_review_propagates_even_when_total_meets_threshold(review, min_confidence):
    payload = response_data()
    payload["dimensions"][0]["review"] = review
    configuration = config()
    configuration["min_confidence"] = min_confidence
    result = execute(RecordingModel(payload), configuration)
    assert result.score == 0.8
    assert result.outcome == Outcome.REVIEW


@pytest.mark.parametrize(
    "failure", [
        "missing", "duplicate", "unknown", "range", "boolean", "review_type", "extra",
        "reason_length", "child_extra",
        pytest.param(("id", "简洁性"), id="translated-id"),
        pytest.param(("id", "CONCISE"), id="renamed-id"),
        pytest.param(("score", "0.5"), id="score-string"),
        pytest.param(("confidence", "0.9"), id="confidence-string"),
        pytest.param(("review", "是"), id="review-chinese-yes"),
        pytest.param(("review", "否"), id="review-chinese-no"),
        pytest.param(("review", 0), id="review-zero"),
        pytest.param(("review", 1), id="review-one"),
        *[
            pytest.param((field, None), id=f"null-{field}")
            for field in ("id", "score", "confidence", "reason", "review")
        ],
        *[
            pytest.param(("missing_field", field), id=f"missing-{field}")
            for field in ("id", "score", "confidence", "reason", "review")
        ],
    ]
)
def test_invalid_response_cannot_be_success_and_records_both_attempts(failure):
    payload = response_data()
    if isinstance(failure, tuple):
        field, value = failure
        if field == "missing_field":
            payload["dimensions"][0].pop(value)
        else:
            payload["dimensions"][0][field] = value
    elif failure == "missing":
        payload["dimensions"].pop()
    elif failure == "duplicate":
        payload["dimensions"][0]["id"] = "correct"
    elif failure == "unknown":
        payload["dimensions"][0]["id"] = "unknown"
    elif failure == "range":
        payload["dimensions"][0]["score"] = 80
    elif failure == "boolean":
        payload["dimensions"][0]["score"] = True
    elif failure == "review_type":
        payload["dimensions"][0]["review"] = "false"
    elif failure == "reason_length":
        payload["dimensions"][0]["reason"] = "证" * (MAX_REASON_CHARS + 1)
    elif failure == "child_extra":
        payload["dimensions"][0]["analysis"] = "extra"
    else:
        payload["score"] = 1
    model = RecordingModel(payload)
    result = execute(model)
    assert result.outcome == Outcome.ERROR
    assert result.score is None
    assert result.error_detail.category == "invalid_output"
    assert len(model.requests) == 2
    assert len(result.judge_record.previous_attempts) == 1
    assert json.loads(result.judge_record.raw_response) == payload
    assert json.loads(result.judge_record.previous_attempts[0].raw_response) == payload


@pytest.mark.parametrize("failure", ["missing", "reason_length", "invalid_json"])
def test_one_correction_can_succeed_and_retains_the_failed_response(failure):
    if failure == "invalid_json":
        invalid_text = "not JSON"
        error_fragment = "not valid JSON"
    elif failure == "reason_length":
        payload = response_data()
        payload["dimensions"][0]["reason"] = "证" * (MAX_REASON_CHARS + 1)
        invalid_text = json.dumps(payload)
        error_fragment = "reason"
    else:
        invalid_text = json.dumps({"dimensions": []})
        error_fragment = "cover every configured dimension"
    model = RecordingModel(
        JudgeResponse(text=invalid_text, resolved_model_id="judge"), response_data(),
    )
    result = execute(model)
    assert result.outcome == Outcome.PASS
    assert len(model.requests) == 2
    assert len(result.judge_record.previous_attempts) == 1
    assert result.judge_record.previous_attempts[0].raw_response == invalid_text
    assert model.requests[1].timeout_seconds <= model.requests[0].timeout_seconds
    first, correction = model.requests
    assert correction.system_prompt.startswith(first.system_prompt)
    assert correction.user_prompt == first.user_prompt
    for request in model.requests:
        assert dimension_response_instructions() in request.system_prompt
        assert "检查事实" in request.system_prompt
    assert "上次响应未通过输出校验" in correction.system_prompt
    assert error_fragment in correction.system_prompt


@pytest.mark.parametrize("first_response", [True, False])
def test_timeout_keeps_attempted_request_and_any_prior_response(first_response):
    model = (
        RecordingModel(JudgeModelTimeout("timeout"))
        if first_response
        else RecordingModel({"dimensions": []}, JudgeModelTimeout("timeout"))
    )
    result = execute(model)
    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "timeout"
    assert result.judge_record.raw_response == ""
    assert result.judge_record.request_system_prompt == model.requests[-1].system_prompt
    assert len(result.judge_record.previous_attempts) == (0 if first_response else 1)


def test_truncated_response_is_not_retried_or_mistaken_for_success():
    model = RecordingModel(
        JudgeResponse(
            text=json.dumps(response_data()),
            resolved_model_id="judge",
            finish_reason="length",
        )
    )
    result = execute(model)
    assert result.outcome == Outcome.ERROR
    assert len(model.requests) == 1
    assert result.judge_record.raw_response


def test_no_output_is_not_applicable_without_model_call():
    model = RecordingModel()
    result = execute(model, execution_trace=trace().model_copy(update={"final_output": {}}))
    assert result.outcome == Outcome.NOT_APPLICABLE
    assert result.judge_record is None
    assert model.requests == []


def test_annotation_shows_recorded_and_reconstructs_version_two_requests():
    model = RecordingModel()
    result = execute(model)
    recorded = evaluator_annotation_evidence(spec(), case(), trace(), result)
    assert recorded["prompt_source"] == "recorded"
    assert recorded["system_prompt"] == model.requests[0].system_prompt
    legacy_record = result.judge_record.model_copy(
        update={
            "request_system_prompt": None,
            "request_user_prompt": None,
        }
    )
    rebuilt = evaluator_annotation_evidence(
        spec(), case(), trace(), result.model_copy(update={"judge_record": legacy_record})
    )
    assert rebuilt["prompt_source"] == "reconstructed"
    assert rebuilt["request_hash_matches"] is True
    assert rebuilt["system_prompt"] == model.requests[0].system_prompt


def test_prompt_uses_actual_dimension_instructions_and_redacted_evidence():
    configuration = config()
    configuration["dimensions"][0]["description"] = "Context supplied to model"
    execution_trace = trace().model_copy(
        update={"final_output": {"answer": "ok", "password": "secret"}}
    )
    request = build_dimension_quality_request(spec(configuration), case(), execution_trace)
    assert "Context supplied to model" in request.system_prompt
    assert "检查事实" in request.system_prompt
    assert "【平台输出协议】" in request.system_prompt
    assert "顶层必须且只能包含 dimensions" in request.system_prompt
    assert "不得翻译、改写或替换为维度名称" in request.system_prompt
    assert "reason 必须为非空字符串，必须使用简体中文" in request.system_prompt
    assert "工具名、代码、标识及必要的原文引用保留原样" in request.system_prompt
    assert "JSON 字段名和固定枚举值保持英文，不得翻译" in request.system_prompt
    assert "硬性上限为 600 字符" in request.system_prompt
    assert "建议不超过 300 字符" in request.system_prompt
    assert "完整响应文本建议不超过 32000 字符" in request.system_prompt
    assert "维度较多时可超过建议值" in request.system_prompt
    assert '"verdict":' not in request.system_prompt
    assert '"violations":' not in request.system_prompt
    assert "hello" in request.user_prompt
    assert "secret" not in request.user_prompt


def management(repository, model):
    return build_default_evaluator_management(
        repository,
        judge_client=model,
        judge_model_id="judge",
        judge_credential_ref="env:JUDGE_KEY",
    )


def api_client(repository, model):
    app = FastAPI()
    dependencies = SimpleNamespace(evaluators=management(repository, model))
    app.dependency_overrides[get_dependencies] = lambda: dependencies
    app.include_router(router)
    return TestClient(app)


def test_api_roundtrip_publication_and_historical_run_keep_exact_config(tmp_path):
    database = tmp_path / "dimensions.db"
    repository = SQLiteRepository(database)
    ensure_demo_dataset(repository)
    ensure_demo_target_descriptors(TargetCatalog(repository))
    configuration = config()
    draft = {
        "kind": "llm_judge",
        "dimension": "answer",
        "metric": "answer_quality",
        "implementation_id": "answer_quality",
        "implementation_version": "2",
        "config": configuration,
    }
    model = RecordingModel()
    with api_client(repository, model) as client:
        response = client.post(
            "/api/evaluators",
            json={
                "name": "真实保存的维度评估",
                "description": "Persistent",
                "draft": draft,
            },
        )
        assert response.status_code == 201, response.text
        evaluator_id = response.json()["evaluator"]["id"]
    repository = SQLiteRepository(database)
    with api_client(repository, model) as client:
        detail = client.get(f"/api/evaluators/{evaluator_id}").json()
        assert detail["draft"]["config"] == configuration
        first_response = client.post(f"/api/evaluators/{evaluator_id}/drafts/publish")
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["version"] == "1"
        assert first["implementation_version"] == "2"
        assert (
            client.patch(f"/api/evaluators/{evaluator_id}", json={"enabled": True}).status_code
            == 200
        )
    evaluators = management(repository, model)
    run = RunManagement(repository, evaluators).create_run(
        build_demo_target_snapshot(get_demo_target_descriptor("loan-agent-v2-fixed")),
        dataset_id=LOAN_DATASET.id,
        evaluator_refs=(EvaluatorRef(evaluator_id=evaluator_id, evaluator_version="1"),),
    )
    second_draft = copy.deepcopy(draft)
    second_draft["config"]["dimensions"][0]["prompt"] = "Changed published instructions"
    with api_client(repository, model) as client:
        assert (
            client.post(
                f"/api/evaluators/{evaluator_id}/drafts", json={"based_on_version": "1"}
            ).status_code
            == 201
        )
        assert (
            client.put(
                f"/api/evaluators/{evaluator_id}/drafts/current", json=second_draft
            ).status_code
            == 200
        )
        second = client.post(f"/api/evaluators/{evaluator_id}/drafts/publish")
        assert second.status_code == 200, second.text
        assert second.json()["version"] == "2"
        assert client.get(f"/api/evaluators/{evaluator_id}/versions/1").json() == first
    restored_run = SQLiteRepository(database).get_run(run.id)
    old_spec = restored_run.manifest.evaluator_specs[0]
    assert old_spec.model_dump(mode="json")["config"] == configuration
    assert old_spec.content_sha256 == first["content_sha256"]
    old_result = evaluators.evaluate_case(case(), trace(), restored_run.manifest.evaluator_specs)[0]
    assert old_result.outcome == Outcome.PASS
    assert "Changed published instructions" not in model.requests[-1].system_prompt


def test_publish_rejects_invalid_dimension_config_and_preserves_draft(tmp_path):
    repository = SQLiteRepository(tmp_path / "invalid-dimensions.db")
    configuration = config()
    configuration["dimensions"][0]["weight"] = 90
    with api_client(repository, RecordingModel()) as client:
        created = client.post(
            "/api/evaluators",
            json={
                "name": "Incomplete",
                "draft": {
                    "kind": "llm_judge",
                    "dimension": "answer",
                    "metric": "answer_quality",
                    "implementation_id": "answer_quality",
                    "implementation_version": "2",
                    "config": configuration,
                },
            },
        )
        evaluator_id = created.json()["evaluator"]["id"]
        assert client.post(f"/api/evaluators/{evaluator_id}/drafts/publish").status_code == 422
        assert client.get(f"/api/evaluators/{evaluator_id}/drafts/current").status_code == 200
