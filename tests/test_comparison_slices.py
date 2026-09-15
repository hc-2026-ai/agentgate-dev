import pytest
from pydantic import ValidationError

from agentgate.result import (
    ComparisonSliceDelta,
    ComparisonSliceDimension,
    ComparisonSliceValue,
    compare_result_breakdowns,
)
from agentgate.result.analytics import AnalyticsBucket, ResultBreakdown


def bucket(
    key: str,
    *,
    label: str | None = None,
    case_count: int = 2,
    passed: int = 1,
    failed: int = 1,
    reviewed: int = 0,
    not_applicable: int = 0,
    errors: int = 0,
    average_score: float | None = 0.5,
) -> AnalyticsBucket:
    observation_count = passed + failed + reviewed + not_applicable + errors
    applicable = passed + failed + reviewed
    return AnalyticsBucket(
        key=key,
        label=label or key,
        case_count=case_count,
        observation_count=observation_count,
        passed=passed,
        failed=failed,
        reviewed=reviewed,
        not_applicable=not_applicable,
        errors=errors,
        applicable=applicable,
        pass_rate=passed / applicable if applicable else None,
        failure_rate=failed / applicable if applicable else None,
        average_score=average_score,
    )


def breakdown(dimension: str, *buckets: AnalyticsBucket) -> ResultBreakdown:
    return ResultBreakdown(
        dimension=dimension,
        available=bool(buckets),
        buckets=buckets,
    )


def test_compares_tag_counts_rates_scores_and_uses_stable_key_order():
    baseline = breakdown(
        "tag",
        bucket("核心", label="核心", passed=1, failed=1, average_score=0.5),
        bucket("边界", label="边界", passed=2, failed=0, average_score=0.9),
    )
    candidate = breakdown(
        "tag",
        bucket("边界", label="边界", passed=1, failed=1, average_score=0.7),
        bucket("核心", label="核心", passed=2, failed=0, average_score=0.8),
    )

    deltas = compare_result_breakdowns(baseline, candidate)

    assert tuple(item.key for item in deltas) == ("核心", "边界")
    core = deltas[0]
    assert core.dimension == "tag"
    assert core.baseline.failed == 1
    assert core.candidate.failed == 0
    assert core.case_count_delta == 0
    assert core.observation_count_delta == 0
    assert core.pass_rate_delta == pytest.approx(0.5)
    assert core.failure_rate_delta == pytest.approx(-0.5)
    assert core.average_score_delta == pytest.approx(0.3)


def test_failure_type_uses_union_and_normalizes_missing_side_to_zero_counts():
    baseline = breakdown("failure_type")
    candidate = breakdown(
        "failure_type",
        bucket(
            "evaluator_error:timeout",
            case_count=1,
            passed=0,
            failed=0,
            errors=1,
            average_score=None,
        ),
    )

    (delta,) = compare_result_breakdowns(baseline, candidate)

    assert delta.dimension == "failure_type"
    assert delta.baseline == ComparisonSliceValue(
        case_count=0,
        observation_count=0,
        passed=0,
        failed=0,
        reviewed=0,
        not_applicable=0,
        errors=0,
        applicable=0,
        pass_rate=None,
        failure_rate=None,
        average_score=None,
    )
    assert delta.candidate.errors == 1
    assert delta.case_count_delta == 1
    assert delta.observation_count_delta == 1
    assert delta.pass_rate_delta is None
    assert delta.failure_rate_delta is None
    assert delta.average_score_delta is None


def test_empty_failure_type_breakdowns_produce_no_deltas():
    empty = breakdown("failure_type")

    assert compare_result_breakdowns(empty, empty) == ()


def test_tag_keys_must_match_instead_of_hiding_missing_evidence_with_zero():
    baseline = breakdown("tag", bucket("core"))
    candidate = breakdown("tag", bucket("other"))

    with pytest.raises(ValueError, match="tag keys must match"):
        compare_result_breakdowns(baseline, candidate)


def test_matching_keys_must_have_matching_labels():
    baseline = breakdown("tag", bucket("core", label="Core"))
    candidate = breakdown("tag", bucket("core", label="核心"))

    with pytest.raises(ValueError, match="conflicting labels"):
        compare_result_breakdowns(baseline, candidate)


def test_breakdowns_must_have_the_same_supported_dimension():
    with pytest.raises(ValueError, match="same dimension"):
        compare_result_breakdowns(
            breakdown("tag", bucket("core")),
            breakdown("failure_type", bucket("core")),
        )
    with pytest.raises(ValueError, match="supports only tag and failure_type"):
        compare_result_breakdowns(
            breakdown("evaluator", bucket("evaluator")),
            breakdown("evaluator", bucket("evaluator")),
        )


def test_slice_value_rejects_incoherent_counts_rates_and_score():
    with pytest.raises(ValidationError, match="observation_count"):
        ComparisonSliceValue(
            case_count=1,
            observation_count=2,
            passed=1,
            failed=0,
            reviewed=0,
            not_applicable=0,
            errors=0,
            applicable=1,
            pass_rate=1,
            failure_rate=0,
            average_score=1,
        )
    with pytest.raises(ValidationError, match="pass_rate"):
        ComparisonSliceValue(
            case_count=1,
            observation_count=1,
            passed=1,
            failed=0,
            reviewed=0,
            not_applicable=0,
            errors=0,
            applicable=1,
            pass_rate=0,
            failure_rate=0,
            average_score=1,
        )
    with pytest.raises(ValidationError, match="average_score"):
        ComparisonSliceValue(
            case_count=0,
            observation_count=0,
            passed=0,
            failed=0,
            reviewed=0,
            not_applicable=0,
            errors=0,
            applicable=0,
            pass_rate=None,
            failure_rate=None,
            average_score=0,
        )


def test_slice_delta_rejects_values_that_do_not_match_snapshots():
    value = ComparisonSliceValue(
        case_count=1,
        observation_count=1,
        passed=1,
        failed=0,
        reviewed=0,
        not_applicable=0,
        errors=0,
        applicable=1,
        pass_rate=1,
        failure_rate=0,
        average_score=1,
    )

    with pytest.raises(ValidationError, match="case_count_delta"):
        ComparisonSliceDelta(
            dimension="tag",
            key="core",
            label="Core",
            baseline=value,
            candidate=value,
            case_count_delta=1,
            observation_count_delta=0,
            pass_rate_delta=0,
            failure_rate_delta=0,
            average_score_delta=0,
        )


def test_comparison_slice_dimension_is_exported_from_result():
    assert ComparisonSliceDimension is not None
