from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentgate.domain import (
    Case,
    CaseTurn,
    CheckResult,
    ComparisonFact,
    DatasetVersion,
    DatasetVersionStatus,
    EvaluationReport,
    EvaluationResult,
    EvaluationRun,
    EvaluatorErrorDetail,
    EvaluatorSpec,
    FailureStage,
    MetricPlan,
    Outcome,
    ReleaseGateSpec,
    RunManifest,
    RunStatus,
    SpanStatus,
    TargetRef,
    TargetSnapshot,
    TargetType,
    Trace,
    TraceSpan,
)
from agentgate.result.analytics import AnalyticsBucket, ResultBreakdown
from agentgate.result.comparison import compare_reports
from agentgate.result.comparison_facts import (
    BadcaseComparison,
    RunComparisonAnalysis,
    compare_badcases,
    derive_comparison_facts,
)
from agentgate.result.comparison_slices import compare_result_breakdowns
from agentgate.result.report import build_evaluation_report


NOW = datetime(2026, 9, 14, tzinfo=UTC)
CASE_IDS = ("case-a", "case-b", "case-c", "case-d")


def _evaluator(evaluator_id: str) -> EvaluatorSpec:
    return EvaluatorSpec(
        id=evaluator_id,
        name=evaluator_id,
        dimension="correctness",
        metric=evaluator_id,
        implementation_id="final_state",
    )


def _dataset() -> DatasetVersion:
    return DatasetVersion(
        id="dataset-version-1",
        dataset_id="dataset-1",
        dataset_name="Comparison Dataset",
        version=1,
        status=DatasetVersionStatus.PUBLISHED,
        cases=tuple(
            Case(
                id=case_id,
                name=case_id,
                turns=(CaseTurn(id=f"turn-{case_id}", input={"message": case_id}),),
                tags=("comparison",),
            )
            for case_id in CASE_IDS
        ),
        created_at=NOW,
        updated_at=NOW,
        published_at=NOW,
    )


def _run(run_id: str, version: str) -> EvaluationRun:
    evaluators = (_evaluator("state"), _evaluator("policy"))
    return EvaluationRun(
        id=run_id,
        manifest=RunManifest(
            dataset=_dataset(),
            target=TargetSnapshot(
                ref=TargetRef(
                    source_id="demo",
                    target_type=TargetType.AGENT,
                    external_target_id="agent",
                    external_version_id=version,
                ),
                display_name="Agent",
                adapter_type="demo",
                adapter_version="1",
                descriptor_sha256="b" * 64,
                captured_at=NOW,
            ),
            evaluator_specs=evaluators,
            primary_evaluator_ids=tuple(item.id for item in evaluators),
            metric_plan=MetricPlan(),
            gate_spec=ReleaseGateSpec(minimum_score=0.5),
            created_at=NOW,
        ),
        status=RunStatus.COMPLETED,
        created_at=NOW,
        started_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
    )


def _result(
    run: EvaluationRun,
    case_id: str,
    evaluator_id: str,
    outcome: Outcome,
) -> EvaluationResult:
    spec = next(item for item in run.manifest.evaluator_specs if item.id == evaluator_id)
    checks: tuple[CheckResult, ...] = ()
    error_detail = None
    score = None
    primary_failure_stage = None
    if outcome is Outcome.ERROR:
        error_detail = EvaluatorErrorDetail(
            category="timeout",
            exception_type="TimeoutError",
            message="timed out",
        )
    else:
        score = {
            Outcome.PASS: 1.0,
            Outcome.FAIL: 0.0,
            Outcome.REVIEW: 0.5,
            Outcome.NOT_APPLICABLE: None,
        }[outcome]
        failure_stage = FailureStage.FINAL_STATE if outcome is Outcome.FAIL else None
        failure_sequence = 1 if outcome is Outcome.FAIL else None
        checks = (
            CheckResult(
                name="check",
                outcome=outcome,
                score=score,
                reason=outcome.value,
                failure_stage=failure_stage,
                failure_sequence=failure_sequence,
            ),
        )
        primary_failure_stage = failure_stage
    return EvaluationResult(
        run_id=run.id,
        case_id=case_id,
        trace_id=(case_id[-1] * 32),
        evaluator_id=spec.id,
        evaluator_name=spec.name,
        evaluator_version=spec.version,
        evaluator_content_sha256=spec.content_sha256,
        evaluator_kind=spec.kind,
        dimension=spec.dimension,
        metric=spec.metric,
        severity=spec.severity,
        outcome=outcome,
        score=score,
        reason=outcome.value,
        checks=checks,
        error_detail=error_detail,
        primary_failure_stage=primary_failure_stage,
    )


def _reports() -> tuple[EvaluationReport, EvaluationReport]:
    baseline_run = _run("baseline-run", "v1")
    candidate_run = _run("candidate-run", "v2")
    baseline = build_evaluation_report(
        baseline_run,
        (
            _result(baseline_run, "case-a", "state", Outcome.PASS),
            _result(baseline_run, "case-b", "state", Outcome.FAIL),
            _result(baseline_run, "case-b", "policy", Outcome.FAIL),
            _result(baseline_run, "case-c", "state", Outcome.REVIEW),
            _result(baseline_run, "case-d", "state", Outcome.ERROR),
        ),
    )
    candidate = build_evaluation_report(
        candidate_run,
        (
            _result(candidate_run, "case-a", "state", Outcome.FAIL),
            _result(candidate_run, "case-a", "policy", Outcome.FAIL),
            _result(candidate_run, "case-b", "state", Outcome.PASS),
            _result(candidate_run, "case-c", "state", Outcome.REVIEW),
            _result(candidate_run, "case-d", "state", Outcome.ERROR),
        ),
    )
    return baseline, candidate


def _trace(run_id: str, case_id: str, duration_ms: float) -> Trace:
    trace_id = case_id[-1] * 32
    return Trace(
        trace_id=trace_id,
        run_id=run_id,
        case_id=case_id,
        spans=(
            TraceSpan(
                trace_id=trace_id,
                span_id=case_id[-1] * 16,
                name="case",
                operation_type="case",
                sequence=0,
                started_at=NOW,
                ended_at=NOW + timedelta(milliseconds=duration_ms),
                status=SpanStatus.OK,
            ),
        ),
    )


def _traces(run_id: str, durations_ms: tuple[float, ...]) -> tuple[Trace, ...]:
    return tuple(
        _trace(run_id, case_id, duration)
        for case_id, duration in zip(CASE_IDS, durations_ms, strict=True)
    )


def _empty_breakdown(dimension: str) -> ResultBreakdown:
    return ResultBreakdown(dimension=dimension, available=False, buckets=())


def test_badcases_use_primary_fail_and_error_results_and_deduplicate_cases() -> None:
    baseline, candidate = _reports()

    badcases = compare_badcases(baseline, candidate)

    assert badcases == BadcaseComparison(
        baseline_case_ids=("case-b", "case-d"),
        candidate_case_ids=("case-a", "case-d"),
        new_case_ids=("case-a",),
        baseline_case_count=2,
        candidate_case_count=2,
        new_case_count=1,
    )


def test_badcase_comparison_rejects_unsorted_or_incoherent_derived_fields() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        BadcaseComparison(
            baseline_case_ids=("case-b", "case-a"),
            candidate_case_ids=(),
            new_case_ids=(),
            baseline_case_count=2,
            candidate_case_count=0,
            new_case_count=0,
        )
    with pytest.raises(ValidationError, match="new_case_ids"):
        BadcaseComparison(
            baseline_case_ids=("case-a",),
            candidate_case_ids=("case-a", "case-b"),
            new_case_ids=(),
            baseline_case_count=1,
            candidate_case_count=2,
            new_case_count=0,
        )


def test_derives_quality_p95_latency_and_new_badcase_facts() -> None:
    baseline, candidate = _reports()
    comparison = compare_reports(baseline, candidate)
    badcases = compare_badcases(baseline, candidate)

    facts = derive_comparison_facts(
        comparison,
        badcases,
        _traces("baseline-run", (100, 200, 300, 400)),
        _traces("candidate-run", (100, 200, 300, 440)),
        CASE_IDS,
    )

    assert tuple(fact.metric_id for fact in facts) == (
        "quality.overall_score_delta",
        "performance.case_latency_p95_change_ratio",
        "badcase.new_case_count",
    )
    quality, performance, badcase = facts
    assert quality.observed_value == comparison.overall_score_delta
    assert performance.baseline_value == pytest.approx(400)
    assert performance.candidate_value == pytest.approx(440)
    assert performance.observed_value == pytest.approx(0.1)
    assert badcase.model_dump() == {
        "metric_id": "badcase.new_case_count",
        "baseline_value": 2.0,
        "candidate_value": 2.0,
        "observed_value": 1.0,
        "availability": "available",
        "unavailable_reason": None,
    }


@pytest.mark.parametrize(
    ("baseline_count", "candidate_count", "reason"),
    (
        (3, 4, "baseline_latency_incomplete"),
        (4, 3, "candidate_latency_incomplete"),
        (3, 3, "both_latency_incomplete"),
    ),
)
def test_missing_case_traces_make_performance_fact_unavailable(
    baseline_count: int,
    candidate_count: int,
    reason: str,
) -> None:
    baseline, candidate = _reports()
    comparison = compare_reports(baseline, candidate)

    facts = derive_comparison_facts(
        comparison,
        compare_badcases(baseline, candidate),
        _traces("baseline-run", (100, 200, 300, 400))[:baseline_count],
        _traces("candidate-run", (100, 200, 300, 400))[:candidate_count],
        CASE_IDS,
    )

    performance = facts[1]
    assert performance.availability == "unavailable"
    assert performance.observed_value is None
    assert performance.unavailable_reason == reason


def test_empty_trace_and_zero_baseline_p95_make_performance_unavailable() -> None:
    baseline, candidate = _reports()
    comparison = compare_reports(baseline, candidate)
    badcases = compare_badcases(baseline, candidate)
    baseline_traces = list(_traces("baseline-run", (0, 0, 0, 0)))

    zero_facts = derive_comparison_facts(
        comparison,
        badcases,
        baseline_traces,
        _traces("candidate-run", (100, 200, 300, 400)),
        CASE_IDS,
    )
    assert zero_facts[1].unavailable_reason == "baseline_latency_zero"

    baseline_traces[0] = baseline_traces[0].model_copy(update={"spans": ()})
    incomplete_facts = derive_comparison_facts(
        comparison,
        badcases,
        baseline_traces,
        _traces("candidate-run", (100, 200, 300, 400)),
        CASE_IDS,
    )
    assert incomplete_facts[1].unavailable_reason == "baseline_latency_incomplete"


def test_trace_identity_and_expected_case_identity_are_validated() -> None:
    baseline, candidate = _reports()
    comparison = compare_reports(baseline, candidate)
    badcases = compare_badcases(baseline, candidate)
    baseline_traces = _traces("baseline-run", (100, 200, 300, 400))
    candidate_traces = _traces("candidate-run", (100, 200, 300, 400))

    with pytest.raises(ValueError, match="different Run"):
        derive_comparison_facts(
            comparison,
            badcases,
            (baseline_traces[0].model_copy(update={"run_id": "wrong"}),),
            candidate_traces,
            CASE_IDS,
        )
    with pytest.raises(ValueError, match="duplicate Case"):
        derive_comparison_facts(
            comparison,
            badcases,
            (*baseline_traces, baseline_traces[0]),
            candidate_traces,
            CASE_IDS,
        )
    with pytest.raises(ValueError, match="expected Case identities"):
        derive_comparison_facts(
            comparison,
            badcases,
            baseline_traces,
            candidate_traces,
            CASE_IDS[:-1],
        )


def test_composite_analysis_enforces_nested_evidence_coherence() -> None:
    baseline, candidate = _reports()
    comparison = compare_reports(baseline, candidate)
    badcases = compare_badcases(baseline, candidate)
    facts = derive_comparison_facts(
        comparison,
        badcases,
        _traces("baseline-run", (100, 200, 300, 400)),
        _traces("candidate-run", (100, 200, 300, 400)),
        CASE_IDS,
    )

    analysis = RunComparisonAnalysis(
        comparison=comparison,
        tag_deltas=compare_result_breakdowns(
            _empty_breakdown("tag"),
            _empty_breakdown("tag"),
        ),
        failure_type_deltas=compare_result_breakdowns(
            _empty_breakdown("failure_type"),
            _empty_breakdown("failure_type"),
        ),
        badcases=badcases,
        facts=facts,
    )
    assert analysis.badcases.new_case_ids == ("case-a",)

    wrong_badcase_fact = facts[2].model_copy(update={"observed_value": 0.0})
    with pytest.raises(ValidationError, match="Badcase fact"):
        RunComparisonAnalysis(
            comparison=comparison,
            tag_deltas=(),
            failure_type_deltas=(),
            badcases=badcases,
            facts=(*facts[:2], wrong_badcase_fact),
        )

    wrong_order: tuple[ComparisonFact, ...] = (facts[1], facts[0], facts[2])
    with pytest.raises(ValidationError, match="fixed metric order"):
        RunComparisonAnalysis(
            comparison=comparison,
            tag_deltas=(),
            failure_type_deltas=(),
            badcases=badcases,
            facts=wrong_order,
        )


def test_comparison_fact_capabilities_are_exported_from_result() -> None:
    from agentgate.result import (
        BadcaseComparison as ExportedBadcaseComparison,
        RunComparisonAnalysis as ExportedRunComparisonAnalysis,
        compare_badcases as exported_compare_badcases,
        derive_comparison_facts as exported_derive_comparison_facts,
    )

    assert ExportedBadcaseComparison is BadcaseComparison
    assert ExportedRunComparisonAnalysis is RunComparisonAnalysis
    assert exported_compare_badcases is compare_badcases
    assert exported_derive_comparison_facts is derive_comparison_facts
