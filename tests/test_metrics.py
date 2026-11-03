import pytest

from pydantic import ValidationError

from agentgate.domain import (
    CheckResult, EvaluationResult, EvaluatorErrorDetail, FailureStage, EvaluatorKind, MetricPlan, MetricSummary,
    Outcome, EvaluatorSeverity,
)
from agentgate.result.metrics import calculate_metrics


def result(case, evaluator, metric, dimension, score, kind=EvaluatorKind.RULE, outcome=None):
    outcome = outcome or (
        Outcome.NOT_APPLICABLE if score is None else Outcome.PASS if score == 1 else Outcome.FAIL
    )
    return EvaluationResult(
        run_id="run",
        trace_id="0" * 32,
        case_id=case,
        evaluator_id=evaluator,
        evaluator_name=evaluator,
        evaluator_version="1",
        evaluator_content_sha256="a" * 64,
        evaluator_kind=kind,
        dimension=dimension,
        metric=metric,
        severity=EvaluatorSeverity.STANDARD,
        outcome=outcome,
        score=score,
        reason="test",
        checks=() if outcome == Outcome.ERROR else (CheckResult(
            name="test", outcome=outcome, score=score, reason="test",
            failure_stage=FailureStage.FINAL_STATE if outcome == Outcome.FAIL else None,
            failure_sequence=0 if outcome == Outcome.FAIL else None,
        ),),
        error_detail=EvaluatorErrorDetail(
            category="timeout", exception_type="TimeoutError", message="test timeout",
        ) if outcome == Outcome.ERROR else None,
        primary_failure_stage=FailureStage.FINAL_STATE if outcome == Outcome.FAIL else None,
    )


def test_metric_dimension_kind_and_overall_paths_do_not_double_count():
    results = [
        result("a", "one", "m1", "tool_use", 1.0),
        result("a", "two", "m2", "tool_use", 0.0),
        result("a", "three", "m3", "state", 1.0),
    ]
    summaries = calculate_metrics(results, ("one", "two", "three"), MetricPlan())
    by_key = {(item.level, item.key): item for item in summaries}
    assert by_key[("dimension", "tool_use")].score == 0.5
    assert by_key[("dimension", "state")].score == 1.0
    assert by_key[("kind", "rule")].score == pytest.approx(2 / 3)
    assert by_key[("overall", "overall")].score == pytest.approx(2 / 3)


@pytest.mark.parametrize("kinds", [
    (EvaluatorKind.RULE, EvaluatorKind.RULE),
    (EvaluatorKind.LLM_GROUP, EvaluatorKind.LLM_GROUP),
    (EvaluatorKind.RULE, EvaluatorKind.LLM_GROUP),
], ids=["rules", "llm-groups", "mixed"])
def test_overall_weights_cases_equally_when_applicable_evaluator_counts_differ(kinds):
    results = [
        result("a", "one", "m1", "answer", 1.0, kinds[0]),
        result("a", "two", "m2", "answer", 1.0, kinds[1]),
        result("b", "one", "m1", "answer", 0.0, kinds[0]),
        result("b", "two", "m2", "answer", None, kinds[1]),
        result("unscored", "one", "m1", "answer", None, kinds[0]),
        result("unscored", "two", "m2", "answer", None, kinds[1]),
        result("a", "dependency", "child", "answer", 0.0),
    ]
    overall = calculate_metrics(results, ("one", "two"), MetricPlan())[0]

    # Case a scores 1, case b scores 0; an entirely unscored case has no numeric weight.
    assert overall.score == 0.5
    assert (overall.passed, overall.failed, overall.not_applicable, overall.total) == (2, 1, 3, 6)


def test_overall_does_not_collapse_primary_evaluators_with_the_same_metric():
    results = [
        result("a", "one", "shared", "answer", 1.0),
        result("a", "two", "shared", "answer", 0.0),
        result("a", "three", "other", "answer", 1.0),
    ]
    summaries = calculate_metrics(results, ("one", "two", "three"), MetricPlan())
    assert summaries[0].score == pytest.approx(2 / 3)
    assert next(item for item in summaries if item.level == "dimension").score == 0.75


def test_overall_matches_the_eight_sample_screenshot_without_rounding_case_scores():
    scores = [
        [None, None, None, 0.25, 1.0],
        [None, None, None, 0.41, 1.0],
        [None, None, None, 0.45, 1.0],
        [None, None, None, 0.60, 1.0],
        [None, None, None, 0.80, 0.0],
        [None, None, 1.0, 0.10, 0.75],
        [1.0, 1.0, None, 0.0, 1.0],
        [None, None, 1.0, 0.0, 1.0],
    ]
    dimensions = ["tool_use", "tool_use", "routing", "answer", "answer"]
    primary = tuple(f"evaluator-{index}" for index in range(5))
    results = [
        result(str(case), primary[index], primary[index], dimensions[index], score,
               EvaluatorKind.LLM_GROUP if index == 4 else EvaluatorKind.RULE)
        for case, row in enumerate(scores)
        for index, score in enumerate(row)
    ]
    overall = calculate_metrics(results, primary, MetricPlan())[0]
    assert overall.score == pytest.approx(0.6610416666666666)


@pytest.mark.parametrize("outcome", [Outcome.NOT_APPLICABLE, Outcome.ERROR])
def test_overall_has_no_score_when_no_case_has_a_numeric_result(outcome):
    results = [result("a", "one", "m1", "answer", None, outcome=outcome)]
    overall = calculate_metrics(results, ("one",), MetricPlan())[0]
    assert overall.score is None
    assert overall.total == 1
    assert calculate_metrics([], ("one",), MetricPlan())[0].score is None


def test_review_and_error_states_are_preserved_while_numeric_scores_are_averaged():
    results = [
        result("a", "one", "m1", "answer", 0.6, outcome=Outcome.REVIEW),
        result("a", "two", "m2", "answer", None, outcome=Outcome.ERROR),
        result("b", "one", "m1", "answer", 1.0),
        result("b", "two", "m2", "answer", 1.0),
    ]
    overall = calculate_metrics(results, ("one", "two"), MetricPlan())[0]
    assert overall.score == 0.8
    assert (overall.reviewed, overall.errors, overall.passed) == (1, 1, 2)


def test_metric_summary_enforces_count_and_score_invariants():
    with pytest.raises(ValidationError, match="total must equal"):
        MetricSummary(
            key="routing", level="metric", score=1.0, passed=1, applicable=1
        )
    with pytest.raises(ValidationError, match="score must exist"):
        MetricSummary(key="routing", level="metric", total=0, score=1.0)
    with pytest.raises(ValidationError, match="overall level and key"):
        MetricSummary(key="overall", level="metric")


def test_metric_contract_keeps_identity_separate_from_display_text():
    assert MetricPlan().model_dump() == {"id": "p1-equal-mean", "version": "1"}
    summary = MetricSummary(
        key="customer_metric",
        level="metric",
        score=1.0,
        passed=1,
        applicable=1,
        total=1,
    )
    assert summary.model_dump()["key"] == "customer_metric"
    assert "label" not in summary.model_dump()


def test_metric_plan_version_must_have_an_implementation():
    with pytest.raises(ValueError, match="unsupported MetricPlan"):
        calculate_metrics([], (), MetricPlan(version="2"))


def test_one_metric_key_cannot_belong_to_multiple_dimensions():
    results = [
        result("a", "one", "shared", "routing", 1.0),
        result("b", "two", "shared", "tool_use", 1.0),
    ]
    with pytest.raises(ValueError, match="belongs to multiple dimensions"):
        calculate_metrics(results, ("one", "two"), MetricPlan())
