"""Default A/B comparison rules and fail-closed rule evaluation."""

from __future__ import annotations

from collections.abc import Iterable

from agentgate.domain import (
    ComparisonConditionDecision,
    ComparisonFact,
    ComparisonGateDecision,
    ComparisonGateRule,
    ComparisonGateSpec,
)


def default_comparison_gate_spec() -> ComparisonGateSpec:
    """Return the service-owned default rules in stable display order."""

    return ComparisonGateSpec(
        rules=(
            ComparisonGateRule(
                metric_id="quality.overall_score_delta",
                operator="gte",
                threshold=-0.02,
            ),
            ComparisonGateRule(
                metric_id="performance.case_latency_p95_change_ratio",
                operator="lte",
                threshold=0.10,
            ),
            ComparisonGateRule(
                metric_id="badcase.new_case_count",
                operator="lte",
                threshold=0,
            ),
        )
    )


def decide_comparison_gate(
    spec: ComparisonGateSpec,
    facts: Iterable[ComparisonFact],
) -> ComparisonGateDecision:
    """Evaluate every submitted rule and fail closed on unavailable evidence."""

    fact_items = tuple(facts)
    fact_ids = tuple(fact.metric_id for fact in fact_items)
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("comparison fact metric ids must be unique")
    facts_by_id = {fact.metric_id: fact for fact in fact_items}

    conditions: list[ComparisonConditionDecision] = []
    for rule in spec.rules:
        fact = facts_by_id.get(rule.metric_id)
        if fact is None or fact.availability == "unavailable":
            conditions.append(
                ComparisonConditionDecision(
                    metric_id=rule.metric_id,
                    operator=rule.operator,
                    threshold=rule.threshold,
                    observed_value=None,
                    status="unavailable",
                    reason_code="fact_unavailable",
                )
            )
            continue

        observed = fact.observed_value
        assert observed is not None
        satisfied = (
            observed >= rule.threshold
            if rule.operator == "gte"
            else observed <= rule.threshold
        )
        conditions.append(
            ComparisonConditionDecision(
                metric_id=rule.metric_id,
                operator=rule.operator,
                threshold=rule.threshold,
                observed_value=observed,
                status="pass" if satisfied else "fail",
                reason_code=(
                    "threshold_met" if satisfied else "threshold_not_met"
                ),
            )
        )

    condition_items = tuple(conditions)
    unmet = tuple(item for item in condition_items if item.status != "pass")
    return ComparisonGateDecision(
        outcome="fail" if unmet else "pass",
        spec=spec,
        conditions=condition_items,
        unmet_conditions=unmet,
    )


__all__ = [
    "decide_comparison_gate",
    "default_comparison_gate_spec",
]
