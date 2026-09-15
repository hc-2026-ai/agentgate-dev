"""Immutable contracts for the fixed A/B comparison launch gate."""

from __future__ import annotations

import math
from typing import Literal, TypeAlias

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .base import DomainModel, require_non_blank


ComparisonMetricId: TypeAlias = Literal[
    "quality.overall_score_delta",
    "performance.case_latency_p95_change_ratio",
    "badcase.new_case_count",
]
ComparisonOperator: TypeAlias = Literal["gte", "lte"]
FactAvailability: TypeAlias = Literal["available", "unavailable"]
ConditionStatus: TypeAlias = Literal["pass", "fail", "unavailable"]
ComparisonGateOutcome: TypeAlias = Literal["pass", "fail"]
ComparisonFactUnavailableReason: TypeAlias = Literal[
    "overall_score_unavailable",
    "baseline_latency_incomplete",
    "candidate_latency_incomplete",
    "both_latency_incomplete",
    "baseline_latency_zero",
]
ComparisonConditionReason: TypeAlias = Literal[
    "threshold_met",
    "threshold_not_met",
    "fact_unavailable",
]


_QUALITY_METRIC = "quality.overall_score_delta"
_PERFORMANCE_METRIC = "performance.case_latency_p95_change_ratio"
_BADCASE_METRIC = "badcase.new_case_count"


def _validate_rule(
    metric_id: ComparisonMetricId,
    operator: ComparisonOperator,
    threshold: float,
) -> None:
    if not math.isfinite(threshold):
        raise ValueError("comparison threshold must be finite")
    if metric_id == _QUALITY_METRIC:
        if operator != "gte":
            raise ValueError("quality.overall_score_delta requires operator gte")
        if not -1 <= threshold <= 1:
            raise ValueError("quality threshold must be between -1 and 1")
    elif metric_id == _PERFORMANCE_METRIC:
        if operator != "lte":
            raise ValueError(
                "performance.case_latency_p95_change_ratio requires operator lte"
            )
        if threshold < -1:
            raise ValueError("performance threshold cannot be less than -1")
    else:
        if operator != "lte":
            raise ValueError("badcase.new_case_count requires operator lte")
        if threshold < 0 or not threshold.is_integer():
            raise ValueError("Badcase threshold must be a non-negative integer")


class ComparisonGateRule(DomainModel):
    """One threshold over a supported comparison fact."""

    metric_id: ComparisonMetricId
    operator: ComparisonOperator
    threshold: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_rule(self) -> "ComparisonGateRule":
        _validate_rule(self.metric_id, self.operator, self.threshold)
        return self


class ComparisonGateSpec(DomainModel):
    """Versioned ordered rules supplied for one comparison decision."""

    id: str = "comparison-gate"
    version: str = "1"
    rules: tuple[ComparisonGateRule, ...]

    @field_validator("id", "version")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return require_non_blank(value, f"ComparisonGateSpec {info.field_name}")

    @field_validator("rules")
    @classmethod
    def validate_rules(
        cls, value: tuple[ComparisonGateRule, ...]
    ) -> tuple[ComparisonGateRule, ...]:
        if not value:
            raise ValueError("ComparisonGateSpec requires at least one rule")
        metric_ids = tuple(item.metric_id for item in value)
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("ComparisonGateSpec metric ids must be unique")
        return value


class ComparisonFact(DomainModel):
    """Available or unavailable evidence for one comparison metric."""

    metric_id: ComparisonMetricId
    baseline_value: float | None = Field(default=None, allow_inf_nan=False)
    candidate_value: float | None = Field(default=None, allow_inf_nan=False)
    observed_value: float | None = Field(default=None, allow_inf_nan=False)
    availability: FactAvailability
    unavailable_reason: ComparisonFactUnavailableReason | None = None

    @model_validator(mode="after")
    def validate_fact(self) -> "ComparisonFact":
        values = (
            self.baseline_value,
            self.candidate_value,
            self.observed_value,
        )
        if self.availability == "available":
            if any(value is None for value in values):
                raise ValueError("available fact requires all values")
            if self.unavailable_reason is not None:
                raise ValueError("available fact cannot have an unavailable reason")
        else:
            if self.observed_value is not None:
                raise ValueError("unavailable fact cannot have an observed value")
            if self.unavailable_reason is None:
                raise ValueError("unavailable fact requires an unavailable reason")
            self._validate_unavailable_reason()
        self._validate_metric_values()
        return self

    def _validate_unavailable_reason(self) -> None:
        quality_reasons = {"overall_score_unavailable"}
        performance_reasons = {
            "baseline_latency_incomplete",
            "candidate_latency_incomplete",
            "both_latency_incomplete",
            "baseline_latency_zero",
        }
        allowed = (
            quality_reasons
            if self.metric_id == _QUALITY_METRIC
            else performance_reasons
            if self.metric_id == _PERFORMANCE_METRIC
            else set()
        )
        if self.unavailable_reason not in allowed:
            raise ValueError("fact unavailable reason does not match its metric")

    def _validate_metric_values(self) -> None:
        values = tuple(
            value
            for value in (
                self.baseline_value,
                self.candidate_value,
                self.observed_value,
            )
            if value is not None
        )
        if self.metric_id == _QUALITY_METRIC:
            scores = (self.baseline_value, self.candidate_value)
            if any(score is not None and not 0 <= score <= 1 for score in scores):
                raise ValueError("quality scores must be between 0 and 1")
            if self.observed_value is not None and not -1 <= self.observed_value <= 1:
                raise ValueError("quality score delta must be between -1 and 1")
        elif self.metric_id == _PERFORMANCE_METRIC:
            latencies = (self.baseline_value, self.candidate_value)
            if any(value is not None and value < 0 for value in latencies):
                raise ValueError("performance latency values must be non-negative")
            if self.observed_value is not None and self.observed_value < -1:
                raise ValueError("performance change ratio cannot be less than -1")
        else:
            if any(value < 0 or not value.is_integer() for value in values):
                raise ValueError("Badcase values must be non-negative integers")
            if (
                self.observed_value is not None
                and self.candidate_value is not None
                and self.observed_value > self.candidate_value
            ):
                raise ValueError(
                    "new Badcase count cannot exceed candidate Badcase count"
                )


class ComparisonConditionDecision(DomainModel):
    """Result of applying one comparison rule to one fact."""

    metric_id: ComparisonMetricId
    operator: ComparisonOperator
    threshold: float = Field(allow_inf_nan=False)
    observed_value: float | None = Field(default=None, allow_inf_nan=False)
    status: ConditionStatus
    reason_code: ComparisonConditionReason

    @model_validator(mode="after")
    def validate_condition(self) -> "ComparisonConditionDecision":
        _validate_rule(self.metric_id, self.operator, self.threshold)
        expected_reason = {
            "pass": "threshold_met",
            "fail": "threshold_not_met",
            "unavailable": "fact_unavailable",
        }[self.status]
        if self.reason_code != expected_reason:
            raise ValueError("condition reason does not match status")
        if self.status == "unavailable":
            if self.observed_value is not None:
                raise ValueError("unavailable condition must not have an observed value")
            return self
        if self.observed_value is None:
            raise ValueError("pass/fail condition requires an observed value")
        satisfied = (
            self.observed_value >= self.threshold
            if self.operator == "gte"
            else self.observed_value <= self.threshold
        )
        if self.status == "pass" and not satisfied:
            raise ValueError("passing condition does not satisfy its rule")
        if self.status == "fail" and satisfied:
            raise ValueError("failing condition does not violate its rule")
        return self


class ComparisonGateDecision(DomainModel):
    """Aggregate fail-closed decision for every submitted comparison rule."""

    outcome: ComparisonGateOutcome
    spec: ComparisonGateSpec
    conditions: tuple[ComparisonConditionDecision, ...]
    unmet_conditions: tuple[ComparisonConditionDecision, ...]

    @model_validator(mode="after")
    def validate_decision(self) -> "ComparisonGateDecision":
        expected_identities = tuple(
            (rule.metric_id, rule.operator, rule.threshold) for rule in self.spec.rules
        )
        actual_identities = tuple(
            (condition.metric_id, condition.operator, condition.threshold)
            for condition in self.conditions
        )
        if actual_identities != expected_identities:
            raise ValueError("comparison conditions must match its rules")
        expected_unmet = tuple(
            condition for condition in self.conditions if condition.status != "pass"
        )
        if self.unmet_conditions != expected_unmet:
            raise ValueError(
                "unmet_conditions must contain every unmet condition in order"
            )
        expected_outcome = "fail" if expected_unmet else "pass"
        if self.outcome != expected_outcome:
            raise ValueError("comparison gate outcome does not match its conditions")
        return self
