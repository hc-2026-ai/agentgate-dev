from fastapi.testclient import TestClient

from agentgate.demo.loan import LOAN_DATASET
from agentgate.domain import EvaluatorKind, EvaluatorSeverity
from agentgate.server.app import create_app


class RecordingDispatcher:
    def __init__(self, fail_calls: set[int] | None = None) -> None:
        self.fail_calls = fail_calls or set()
        self.run_ids: list[str] = []

    def submit(self, run_id: str) -> None:
        self.run_ids.append(run_id)
        if len(self.run_ids) in self.fail_calls:
            raise ConnectionError("redis password=secret")


def _launch_payload() -> dict[str, object]:
    return {
        "baseline_version": "loan-agent-v1-risky",
        "candidate_version": "loan-agent-v2-fixed",
        "dataset_id": LOAN_DATASET.id,
        "dataset_version": 1,
        "evaluators": [
            {"id": "skill-routing", "version": "1"},
            {"id": "final-state", "version": "1"},
        ],
    }


def test_launch_run_comparison_creates_and_dispatches_controlled_pair(
    tmp_path,
) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-launch.db", dispatcher)

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=_launch_payload())

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"baseline", "candidate"}
    assert set(body["baseline"]) == {"run_id", "status"}
    assert set(body["candidate"]) == {"run_id", "status"}
    assert body["baseline"]["status"] == "pending"
    assert body["candidate"]["status"] == "pending"
    assert dispatcher.run_ids == [
        body["baseline"]["run_id"],
        body["candidate"]["run_id"],
    ]

    repository = application.state.dependencies.repository
    baseline = repository.get_run(body["baseline"]["run_id"])
    candidate = repository.get_run(body["candidate"]["run_id"])
    assert baseline is not None
    assert candidate is not None
    assert baseline.manifest.dataset == candidate.manifest.dataset
    assert baseline.manifest.evaluator_specs == candidate.manifest.evaluator_specs


def test_launch_run_comparison_selects_exact_historical_evaluator_version(
    tmp_path,
) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-exact-evaluator.db", dispatcher)
    evaluators = application.state.dependencies.evaluators
    evaluator, _ = evaluators.create_evaluator(
        "Versioned comparison output",
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="comparison_output_v1",
        implementation_id="final_output",
        config={},
    )
    first = evaluators.publish_draft(evaluator.id)
    evaluators.update_evaluator(evaluator.id, enabled=True)
    evaluators.create_draft(evaluator.id)
    evaluators.replace_draft(
        evaluator.id,
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="comparison_output_v2",
        severity=EvaluatorSeverity.BLOCKING,
        implementation_id="final_output",
        implementation_version="1",
        config={},
        children=(),
        combination=None,
    )
    second = evaluators.publish_draft(evaluator.id)
    payload = _launch_payload()
    payload["evaluators"] = [{"id": evaluator.id, "version": first.version}]

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=payload)

    assert response.status_code == 202
    assert second.version == "2"
    baseline = application.state.dependencies.repository.get_run(
        response.json()["baseline"]["run_id"]
    )
    candidate = application.state.dependencies.repository.get_run(
        response.json()["candidate"]["run_id"]
    )
    assert baseline is not None
    assert candidate is not None
    assert baseline.manifest.evaluator_specs == (first,)
    assert candidate.manifest.evaluator_specs == (first,)


def test_launch_run_comparison_rejects_unknown_evaluator_version(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-unknown-evaluator.db")
    payload = _launch_payload()
    payload["evaluators"] = [{"id": "final-state", "version": "999"}]

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "unknown Evaluator version: final-state@999"
    )
    assert application.state.dependencies.repository.list_runs() == []


def test_launch_run_comparison_rejects_identical_versions(tmp_path) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-same-version.db", dispatcher)
    payload = _launch_payload()
    payload["candidate_version"] = payload["baseline_version"]

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "A/B variants must use different Agent versions"
    )
    assert dispatcher.run_ids == []
    assert application.state.dependencies.repository.list_runs() == []


def test_launch_run_comparison_rejects_unknown_demo_version(tmp_path) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-unknown-version.db", dispatcher)
    payload = _launch_payload()
    payload["candidate_version"] = "unknown-version"

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "unknown demo Target version: unknown-version"
    )
    assert dispatcher.run_ids == []
    assert application.state.dependencies.repository.list_runs() == []


def test_launch_run_comparison_rejects_raw_target_configuration(tmp_path) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-extra-input.db", dispatcher)
    payload = _launch_payload()
    payload["invocation_config"] = {"api_key": "must-not-be-accepted"}

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=payload)

    assert response.status_code == 422
    assert dispatcher.run_ids == []
    assert application.state.dependencies.repository.list_runs() == []


def test_launch_run_comparison_returns_partial_dispatch_states(tmp_path) -> None:
    dispatcher = RecordingDispatcher(fail_calls={1})
    application = create_app(tmp_path / "comparison-partial-dispatch.db", dispatcher)

    with TestClient(application) as client:
        response = client.post("/api/run-comparisons", json=_launch_payload())

    assert response.status_code == 202
    body = response.json()
    assert dispatcher.run_ids == [
        body["baseline"]["run_id"],
        body["candidate"]["run_id"],
    ]
    assert body["baseline"]["status"] == "failed"
    assert body["candidate"]["status"] == "pending"
    assert "secret" not in response.text

    repository = application.state.dependencies.repository
    baseline = repository.get_run(body["baseline"]["run_id"])
    candidate = repository.get_run(body["candidate"]["run_id"])
    assert baseline is not None
    assert candidate is not None
    assert baseline.error == "Run dispatch failed: ConnectionError"
    assert candidate.error is None


def test_compare_completed_runs(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-api.db")
    dependencies = application.state.dependencies
    baseline = dependencies.execute_demo_run("loan-agent-v1-risky")
    candidate = dependencies.execute_demo_run("loan-agent-v2-fixed")

    with TestClient(application) as client:
        response = client.get(
            "/api/run-comparisons",
            params={
                "baseline_run_id": baseline.id,
                "candidate_run_id": candidate.id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["baseline_run_id"] == baseline.id
    assert body["candidate_run_id"] == candidate.id
    assert body["baseline_target_version"] == "loan-agent-v1-risky"
    assert body["candidate_target_version"] == "loan-agent-v2-fixed"
    assert body["overall_score_delta"] > 0
    assert any(item["change"] == "improvement" for item in body["case_deltas"])
    assert "facts" not in body
    assert "badcases" not in body


def test_unknown_comparison_run_returns_not_found(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-missing.db")

    with TestClient(application) as client:
        response = client.get(
            "/api/run-comparisons",
            params={
                "baseline_run_id": "missing-baseline",
                "candidate_run_id": "missing-candidate",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown EvaluationRun: missing-baseline"


def test_incomplete_comparison_run_returns_conflict(tmp_path) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-incomplete.db", dispatcher)
    dependencies = application.state.dependencies
    baseline = dependencies.execute_demo_run("loan-agent-v1-risky")
    candidate = dependencies.submit_demo_run("loan-agent-v2-fixed")

    with TestClient(application) as client:
        response = client.get(
            "/api/run-comparisons",
            params={
                "baseline_run_id": baseline.id,
                "candidate_run_id": candidate.id,
            },
        )

    assert response.status_code == 409
    assert dispatcher.run_ids == [candidate.id]
    assert response.json()["detail"] == (
        "EvaluationReport requires a completed EvaluationRun"
    )


def test_incompatible_comparison_runs_return_conflict(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-incompatible.db")
    dependencies = application.state.dependencies
    baseline = dependencies.execute_demo_run(
        "loan-agent-v1-risky",
        evaluator_ids=["skill-routing"],
    )
    candidate = dependencies.execute_demo_run(
        "loan-agent-v2-fixed",
        evaluator_ids=["final-state"],
    )

    with TestClient(application) as client:
        response = client.get(
            "/api/run-comparisons",
            params={
                "baseline_run_id": baseline.id,
                "candidate_run_id": candidate.id,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "reports use different primary Evaluators"


def test_get_complete_run_comparison_analysis(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-analysis-api.db")
    dependencies = application.state.dependencies
    baseline = dependencies.execute_demo_run("loan-agent-v1-risky")
    candidate = dependencies.execute_demo_run("loan-agent-v2-fixed")

    with TestClient(application) as client:
        response = client.get(
            "/api/run-comparisons/analysis",
            params={
                "baseline_run_id": baseline.id,
                "candidate_run_id": candidate.id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["comparison"]["baseline_run_id"] == baseline.id
    assert body["comparison"]["candidate_run_id"] == candidate.id
    assert all(item["dimension"] == "tag" for item in body["tag_deltas"])
    assert all(
        item["dimension"] == "failure_type"
        for item in body["failure_type_deltas"]
    )
    assert set(body["badcases"]) == {
        "baseline_case_ids",
        "candidate_case_ids",
        "new_case_ids",
        "baseline_case_count",
        "candidate_case_count",
        "new_case_count",
    }
    assert [fact["metric_id"] for fact in body["facts"]] == [
        "quality.overall_score_delta",
        "performance.case_latency_p95_change_ratio",
        "badcase.new_case_count",
    ]


def test_get_comparison_gate_defaults(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-gate-defaults-api.db")

    with TestClient(application) as client:
        response = client.get("/api/run-comparisons/gate-defaults")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "comparison-gate"
    assert body["version"] == "1"
    assert [
        (rule["metric_id"], rule["operator"], rule["threshold"])
        for rule in body["rules"]
    ] == [
        ("quality.overall_score_delta", "gte", -0.02),
        ("performance.case_latency_p95_change_ratio", "lte", 0.1),
        ("badcase.new_case_count", "lte", 0.0),
    ]


def test_comparison_gate_business_pass_and_fail_both_return_ok(
    tmp_path,
    monkeypatch,
) -> None:
    application = create_app(tmp_path / "comparison-gate-api.db")
    dependencies = application.state.dependencies
    baseline = dependencies.execute_demo_run("loan-agent-v1-risky")
    candidate = dependencies.execute_demo_run("loan-agent-v2-fixed")
    passing_payload = {
        "baseline_run_id": baseline.id,
        "candidate_run_id": candidate.id,
        "gate_spec": {
            "id": "custom-gate",
            "version": "1",
            "rules": [
                {
                    "metric_id": "quality.overall_score_delta",
                    "operator": "gte",
                    "threshold": -1,
                }
            ],
        },
    }

    with TestClient(application) as client:
        passed = client.post("/api/run-comparisons/gate", json=passing_payload)

        original_list_traces = dependencies.repository.list_traces

        def missing_candidate_traces(run_id):
            if run_id == candidate.id:
                return []
            return original_list_traces(run_id)

        monkeypatch.setattr(
            dependencies.repository,
            "list_traces",
            missing_candidate_traces,
        )
        failing_payload = {
            **passing_payload,
            "gate_spec": {
                "id": "custom-gate",
                "version": "1",
                "rules": [
                    {
                        "metric_id": "quality.overall_score_delta",
                        "operator": "gte",
                        "threshold": 1,
                    },
                    {
                        "metric_id": (
                            "performance.case_latency_p95_change_ratio"
                        ),
                        "operator": "lte",
                        "threshold": 0.1,
                    },
                ],
            },
        }
        failed = client.post("/api/run-comparisons/gate", json=failing_payload)

    assert passed.status_code == 200
    assert passed.json()["outcome"] == "pass"
    assert failed.status_code == 200
    failed_body = failed.json()
    assert failed_body["outcome"] == "fail"
    assert [item["status"] for item in failed_body["conditions"]] == [
        "fail",
        "unavailable",
    ]
    assert failed_body["unmet_conditions"] == failed_body["conditions"]


def test_comparison_gate_rejects_unknown_fields_and_invalid_rules(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-gate-validation-api.db")
    base = {
        "baseline_run_id": "baseline",
        "candidate_run_id": "candidate",
        "gate_spec": {
            "rules": [
                {
                    "metric_id": "badcase.new_case_count",
                    "operator": "lte",
                    "threshold": 0,
                }
            ]
        },
    }

    with TestClient(application) as client:
        extra = client.post(
            "/api/run-comparisons/gate",
            json={**base, "enabled": True},
        )
        invalid = client.post(
            "/api/run-comparisons/gate",
            json={
                **base,
                "gate_spec": {
                    "rules": [
                        {
                            "metric_id": "badcase.new_case_count",
                            "operator": "lte",
                            "threshold": -1,
                        }
                    ]
                },
            },
        )

    assert extra.status_code == 422
    assert invalid.status_code == 422


def test_comparison_analysis_and_gate_map_application_errors(tmp_path) -> None:
    dispatcher = RecordingDispatcher()
    application = create_app(tmp_path / "comparison-new-errors-api.db", dispatcher)
    dependencies = application.state.dependencies
    completed = dependencies.execute_demo_run("loan-agent-v1-risky")
    pending = dependencies.submit_demo_run("loan-agent-v2-fixed")
    gate_spec = {
        "rules": [
            {
                "metric_id": "quality.overall_score_delta",
                "operator": "gte",
                "threshold": -0.02,
            }
        ]
    }

    with TestClient(application) as client:
        missing = client.get(
            "/api/run-comparisons/analysis",
            params={
                "baseline_run_id": "missing",
                "candidate_run_id": completed.id,
            },
        )
        incomplete = client.post(
            "/api/run-comparisons/gate",
            json={
                "baseline_run_id": completed.id,
                "candidate_run_id": pending.id,
                "gate_spec": gate_spec,
            },
        )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "unknown EvaluationRun: missing"
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"] == (
        "Run comparison analysis requires completed EvaluationRuns"
    )


def test_comparison_openapi_exposes_analysis_and_gate_contracts(tmp_path) -> None:
    application = create_app(tmp_path / "comparison-openapi.db")

    with TestClient(application) as client:
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert "/api/run-comparisons/analysis" in paths
    assert "/api/run-comparisons/gate-defaults" in paths
    assert "/api/run-comparisons/gate" in paths
    components = schema["components"]["schemas"]
    assert "RunComparisonAnalysis" in components
    assert "ComparisonGateSpec" in components
    assert "ComparisonGateDecision" in components
    request_schema = components["RunComparisonGateRequest"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {
        "baseline_run_id",
        "candidate_run_id",
        "gate_spec",
    }
