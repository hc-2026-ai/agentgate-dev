from __future__ import annotations

import pytest

from agentgate.domain import (
    ComparisonFact,
    ComparisonGateRule,
    ComparisonGateSpec,
)
from agentgate.result.comparison_gate import (
    decide_comparison_gate,
    default_comparison_gate_spec,
)


def _facts() -> tuple[ComparisonFact, ...]:
    return (
        ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=0.80,
            candidate_value=0.79,
            observed_value=-0.01,
            availability="available",
        ),
        ComparisonFact(
            metric_id="performance.case_latency_p95_change_ratio",
            baseline_value=100,
            candidate_value=108,
            observed_value=0.08,
            availability="available",
        ),
        ComparisonFact(
            metric_id="badcase.new_case_count",
            baseline_value=2,
            candidate_value=2,
            observed_value=0,
            availability="available",
        ),
    )


def test_default_spec_contains_the_three_confirmed_rules_in_stable_order() -> None:
    spec = default_comparison_gate_spec()

    assert spec.id == "comparison-gate"
    assert spec.version == "1"
    assert tuple(
        (rule.metric_id, rule.operator, rule.threshold) for rule in spec.rules
    ) == (
        ("quality.overall_score_delta", "gte", -0.02),
        ("performance.case_latency_p95_change_ratio", "lte", 0.10),
        ("badcase.new_case_count", "lte", 0.0),
    )


def test_all_default_conditions_pass_at_or_within_thresholds() -> None:
    decision = decide_comparison_gate(default_comparison_gate_spec(), _facts())

    assert decision.outcome == "pass"
    assert tuple(condition.status for condition in decision.conditions) == (
        "pass",
        "pass",
        "pass",
    )
    assert all(
        condition.reason_code == "threshold_met"
        for condition in decision.conditions
    )
    assert decision.unmet_conditions == ()


@pytest.mark.parametrize(
    ("fact_index", "observed_value"),
    (
        (0, -0.021),
        (1, 0.101),
        (2, 1.0),
    ),
)
def test_each_default_rule_can_fail(
    fact_index: int,
    observed_value: float,
) -> None:
    facts = list(_facts())
    facts[fact_index] = facts[fact_index].model_copy(
        update={"observed_value": observed_value}
    )

    decision = decide_comparison_gate(default_comparison_gate_spec(), facts)

    assert decision.outcome == "fail"
    assert decision.conditions[fact_index].status == "fail"
    assert decision.conditions[fact_index].reason_code == "threshold_not_met"
    assert decision.unmet_conditions == (decision.conditions[fact_index],)


def test_returns_every_failed_and_unavailable_condition_in_rule_order() -> None:
    facts = list(_facts())
    facts[0] = facts[0].model_copy(update={"observed_value": -0.03})
    facts[1] = ComparisonFact(
        metric_id="performance.case_latency_p95_change_ratio",
        baseline_value=100,
        candidate_value=None,
        availability="unavailable",
        unavailable_reason="candidate_latency_incomplete",
    )
    facts[2] = facts[2].model_copy(update={"observed_value": 2.0})

    decision = decide_comparison_gate(default_comparison_gate_spec(), facts)

    assert decision.outcome == "fail"
    assert tuple(condition.status for condition in decision.conditions) == (
        "fail",
        "unavailable",
        "fail",
    )
    assert decision.unmet_conditions == decision.conditions


def test_missing_required_fact_is_unavailable_and_fails_closed() -> None:
    facts = tuple(
        fact
        for fact in _facts()
        if fact.metric_id != "performance.case_latency_p95_change_ratio"
    )

    decision = decide_comparison_gate(default_comparison_gate_spec(), facts)

    condition = decision.conditions[1]
    assert decision.outcome == "fail"
    assert condition.metric_id == "performance.case_latency_p95_change_ratio"
    assert condition.observed_value is None
    assert condition.status == "unavailable"
    assert condition.reason_code == "fact_unavailable"


def test_unsubmitted_performance_rule_does_not_require_performance_fact() -> None:
    default = default_comparison_gate_spec()
    spec = ComparisonGateSpec(rules=(default.rules[0], default.rules[2]))
    facts = tuple(
        fact
        for fact in _facts()
        if fact.metric_id != "performance.case_latency_p95_change_ratio"
    )

    decision = decide_comparison_gate(spec, facts)

    assert decision.outcome == "pass"
    assert tuple(condition.metric_id for condition in decision.conditions) == (
        "quality.overall_score_delta",
        "badcase.new_case_count",
    )


def test_condition_order_follows_submitted_rule_order() -> None:
    default = default_comparison_gate_spec()
    spec = ComparisonGateSpec(
        rules=(default.rules[2], default.rules[0], default.rules[1])
    )

    decision = decide_comparison_gate(spec, reversed(_facts()))

    assert tuple(condition.metric_id for condition in decision.conditions) == tuple(
        rule.metric_id for rule in spec.rules
    )


def test_duplicate_facts_are_rejected_even_when_their_rule_is_not_submitted() -> None:
    quality = _facts()[0]
    spec = ComparisonGateSpec(
        rules=(
            ComparisonGateRule(
                metric_id="badcase.new_case_count",
                operator="lte",
                threshold=0,
            ),
        )
    )

    with pytest.raises(ValueError, match="fact metric ids must be unique"):
        decide_comparison_gate(spec, (quality, quality, _facts()[2]))


def test_comparison_gate_functions_are_exported_from_result() -> None:
    from agentgate.result import (
        decide_comparison_gate as exported_decide_comparison_gate,
        default_comparison_gate_spec as exported_default_comparison_gate_spec,
    )

    assert exported_decide_comparison_gate is decide_comparison_gate
    assert exported_default_comparison_gate_spec is default_comparison_gate_spec
