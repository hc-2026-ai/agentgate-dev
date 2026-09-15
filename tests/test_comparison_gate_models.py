import math

import pytest
from pydantic import ValidationError

from agentgate.domain import (
    ComparisonConditionDecision,
    ComparisonConditionReason,
    ComparisonFact,
    ComparisonFactUnavailableReason,
    ComparisonGateDecision,
    ComparisonGateOutcome,
    ComparisonGateRule,
    ComparisonGateSpec,
    ComparisonMetricId,
    ComparisonOperator,
    ConditionStatus,
    FactAvailability,
)


def rule(
    metric_id: str = "quality.overall_score_delta",
    operator: str = "gte",
    threshold: float = -0.02,
) -> ComparisonGateRule:
    return ComparisonGateRule(
        metric_id=metric_id,
        operator=operator,
        threshold=threshold,
    )


def condition(
    *,
    status: str = "pass",
    reason_code: str = "threshold_met",
    observed_value: float | None = 0.0,
    gate_rule: ComparisonGateRule | None = None,
) -> ComparisonConditionDecision:
    selected = gate_rule or rule()
    return ComparisonConditionDecision(
        metric_id=selected.metric_id,
        operator=selected.operator,
        threshold=selected.threshold,
        observed_value=observed_value,
        status=status,
        reason_code=reason_code,
    )


@pytest.mark.parametrize(
    ("metric_id", "operator", "threshold"),
    [
        ("quality.overall_score_delta", "gte", -0.02),
        ("performance.case_latency_p95_change_ratio", "lte", 0.1),
        ("badcase.new_case_count", "lte", 0),
    ],
)
def test_rule_accepts_the_three_supported_metrics(metric_id, operator, threshold):
    created = rule(metric_id, operator, threshold)

    assert created.metric_id == metric_id
    assert created.operator == operator
    assert created.threshold == threshold


@pytest.mark.parametrize(
    ("metric_id", "operator", "threshold", "message"),
    [
        ("quality.overall_score_delta", "lte", 0, "requires operator gte"),
        ("quality.overall_score_delta", "gte", -1.01, "between -1 and 1"),
        ("quality.overall_score_delta", "gte", 1.01, "between -1 and 1"),
        (
            "performance.case_latency_p95_change_ratio",
            "gte",
            0.1,
            "requires operator lte",
        ),
        (
            "performance.case_latency_p95_change_ratio",
            "lte",
            -1.01,
            "cannot be less than -1",
        ),
        ("badcase.new_case_count", "gte", 0, "requires operator lte"),
        ("badcase.new_case_count", "lte", -1, "non-negative integer"),
        ("badcase.new_case_count", "lte", 0.5, "non-negative integer"),
    ],
)
def test_rule_rejects_metric_specific_operator_and_threshold_errors(
    metric_id,
    operator,
    threshold,
    message,
):
    with pytest.raises(ValidationError, match=message):
        rule(metric_id, operator, threshold)


@pytest.mark.parametrize("threshold", [math.nan, math.inf, -math.inf])
def test_rule_rejects_non_finite_thresholds(threshold):
    with pytest.raises(ValidationError):
        rule(threshold=threshold)


def test_gate_spec_requires_nonblank_identity_and_unique_nonempty_rules():
    quality = rule()

    with pytest.raises(ValidationError, match="must not be blank"):
        ComparisonGateSpec(id=" ", rules=(quality,))
    with pytest.raises(ValidationError, match="at least one rule"):
        ComparisonGateSpec(rules=())
    with pytest.raises(ValidationError, match="metric ids must be unique"):
        ComparisonGateSpec(rules=(quality, quality))


def test_available_fact_requires_complete_values_and_no_unavailable_reason():
    created = ComparisonFact(
        metric_id="quality.overall_score_delta",
        baseline_value=0.9,
        candidate_value=0.88,
        observed_value=-0.02,
        availability="available",
    )

    assert created.observed_value == -0.02
    with pytest.raises(ValidationError, match="available fact requires all values"):
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.9,
            candidate_value=None,
            observed_value=-0.02,
            availability="available",
        )
    with pytest.raises(ValidationError, match="cannot have an unavailable reason"):
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.9,
            candidate_value=0.88,
            observed_value=-0.02,
            availability="available",
            unavailable_reason="overall_score_unavailable",
        )


def test_unavailable_fact_requires_no_observed_value_and_a_compatible_reason():
    created = ComparisonFact(
        metric_id="performance.case_latency_p95_change_ratio",
        baseline_value=100,
        candidate_value=None,
        observed_value=None,
        availability="unavailable",
        unavailable_reason="candidate_latency_incomplete",
    )

    assert created.unavailable_reason == "candidate_latency_incomplete"
    with pytest.raises(ValidationError, match="requires an unavailable reason"):
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.9,
            candidate_value=None,
            observed_value=None,
            availability="unavailable",
        )
    with pytest.raises(ValidationError, match="cannot have an observed value"):
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.9,
            candidate_value=None,
            observed_value=-0.1,
            availability="unavailable",
            unavailable_reason="overall_score_unavailable",
        )
    with pytest.raises(ValidationError, match="unavailable reason does not match"):
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.9,
            candidate_value=None,
            observed_value=None,
            availability="unavailable",
            unavailable_reason="baseline_latency_incomplete",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "metric_id": "quality.overall_score_delta",
            "baseline_value": 1.1,
            "candidate_value": 0.9,
            "observed_value": -0.2,
        },
        {
            "metric_id": "performance.case_latency_p95_change_ratio",
            "baseline_value": -1,
            "candidate_value": 10,
            "observed_value": 0.1,
        },
        {
            "metric_id": "badcase.new_case_count",
            "baseline_value": 1,
            "candidate_value": 2,
            "observed_value": 2.5,
        },
        {
            "metric_id": "badcase.new_case_count",
            "baseline_value": 1,
            "candidate_value": 2,
            "observed_value": 3,
        },
    ],
)
def test_fact_rejects_metric_specific_invalid_values(kwargs):
    with pytest.raises(ValidationError):
        ComparisonFact(availability="available", **kwargs)


@pytest.mark.parametrize(
    ("status", "reason_code", "observed_value", "message"),
    [
        ("pass", "threshold_not_met", 0.0, "reason does not match status"),
        ("fail", "threshold_met", -0.03, "reason does not match status"),
        ("unavailable", "fact_unavailable", 0.0, "must not have an observed value"),
        ("pass", "threshold_met", None, "requires an observed value"),
        ("pass", "threshold_met", -0.03, "does not satisfy its rule"),
        ("fail", "threshold_not_met", 0.0, "does not violate its rule"),
    ],
)
def test_condition_decision_rejects_incoherent_status(
    status,
    reason_code,
    observed_value,
    message,
):
    with pytest.raises(ValidationError, match=message):
        condition(
            status=status,
            reason_code=reason_code,
            observed_value=observed_value,
        )


def test_gate_decision_requires_conditions_to_match_rules_and_all_unmet_items():
    quality = rule()
    badcase = rule("badcase.new_case_count", "lte", 0)
    spec = ComparisonGateSpec(rules=(quality, badcase))
    passed = condition(gate_rule=quality)
    failed = condition(
        gate_rule=badcase,
        status="fail",
        reason_code="threshold_not_met",
        observed_value=1,
    )
    decision = ComparisonGateDecision(
        outcome="fail",
        spec=spec,
        conditions=(passed, failed),
        unmet_conditions=(failed,),
    )

    assert decision.unmet_conditions == (failed,)
    with pytest.raises(ValidationError, match="must match its rules"):
        ComparisonGateDecision(
            outcome="fail",
            spec=spec,
            conditions=(failed, passed),
            unmet_conditions=(failed,),
        )
    with pytest.raises(ValidationError, match="must contain every unmet condition"):
        ComparisonGateDecision(
            outcome="fail",
            spec=spec,
            conditions=(passed, failed),
            unmet_conditions=(),
        )
    with pytest.raises(ValidationError, match="outcome does not match"):
        ComparisonGateDecision(
            outcome="pass",
            spec=spec,
            conditions=(passed, failed),
            unmet_conditions=(failed,),
        )


def test_models_are_frozen_and_reject_extra_fields():
    created = rule()

    with pytest.raises(ValidationError):
        created.threshold = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ComparisonGateRule(
            metric_id="quality.overall_score_delta",
            operator="gte",
            threshold=-0.02,
            unknown=True,
        )


def test_comparison_gate_public_types_are_exported_from_domain():
    exported = (
        ComparisonConditionReason,
        ComparisonFactUnavailableReason,
        ComparisonGateOutcome,
        ComparisonMetricId,
        ComparisonOperator,
        ConditionStatus,
        FactAvailability,
    )

    assert all(item is not None for item in exported)
