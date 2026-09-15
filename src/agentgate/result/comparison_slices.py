"""Deterministic deltas between existing Result analytics breakdowns."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, ValidationInfo, field_validator, model_validator

from agentgate.domain import DomainModel
from agentgate.domain.base import require_non_blank
from agentgate.result.analytics import AnalyticsBucket, ResultBreakdown


ComparisonSliceDimension: TypeAlias = Literal["tag", "failure_type"]


class ComparisonSliceValue(DomainModel):
    """Normalized analytics values for one side of a comparison slice."""

    case_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    reviewed: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    errors: int = Field(ge=0)
    applicable: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    average_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_values(self) -> "ComparisonSliceValue":
        if self.observation_count != (
            self.passed
            + self.failed
            + self.reviewed
            + self.not_applicable
            + self.errors
        ):
            raise ValueError("observation_count must equal all outcome counts")
        if self.applicable != self.passed + self.failed + self.reviewed:
            raise ValueError("applicable must equal passed + failed + reviewed")
        if self.case_count > self.observation_count:
            raise ValueError("case_count cannot exceed observation_count")
        expected_pass_rate = self.passed / self.applicable if self.applicable else None
        expected_failure_rate = (
            self.failed / self.applicable if self.applicable else None
        )
        if self.pass_rate != expected_pass_rate:
            raise ValueError("pass_rate does not match outcome counts")
        if self.failure_rate != expected_failure_rate:
            raise ValueError("failure_rate does not match outcome counts")
        if (self.average_score is None) != (self.applicable == 0):
            raise ValueError("average_score must exist exactly for applicable observations")
        return self


class ComparisonSliceDelta(DomainModel):
    """Baseline, candidate, and delta values for one analytics slice."""

    dimension: ComparisonSliceDimension
    key: str
    label: str
    baseline: ComparisonSliceValue
    candidate: ComparisonSliceValue
    case_count_delta: int
    observation_count_delta: int
    pass_rate_delta: float | None = Field(default=None, allow_inf_nan=False)
    failure_rate_delta: float | None = Field(default=None, allow_inf_nan=False)
    average_score_delta: float | None = Field(default=None, allow_inf_nan=False)

    @field_validator("key", "label")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return require_non_blank(value, f"ComparisonSliceDelta {info.field_name}")

    @model_validator(mode="after")
    def validate_deltas(self) -> "ComparisonSliceDelta":
        if self.case_count_delta != (
            self.candidate.case_count - self.baseline.case_count
        ):
            raise ValueError("case_count_delta does not match its snapshots")
        if self.observation_count_delta != (
            self.candidate.observation_count - self.baseline.observation_count
        ):
            raise ValueError("observation_count_delta does not match its snapshots")
        expected = (
            _optional_delta(self.baseline.pass_rate, self.candidate.pass_rate),
            _optional_delta(self.baseline.failure_rate, self.candidate.failure_rate),
            _optional_delta(self.baseline.average_score, self.candidate.average_score),
        )
        actual = (
            self.pass_rate_delta,
            self.failure_rate_delta,
            self.average_score_delta,
        )
        if actual != expected:
            raise ValueError("rate and score deltas do not match their snapshots")
        return self


def _optional_delta(
    baseline: float | None,
    candidate: float | None,
) -> float | None:
    if baseline is None or candidate is None:
        return None
    return candidate - baseline


def _slice_value(bucket: AnalyticsBucket | None) -> ComparisonSliceValue:
    if bucket is None:
        return ComparisonSliceValue(
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
    return ComparisonSliceValue(
        case_count=bucket.case_count,
        observation_count=bucket.observation_count,
        passed=bucket.passed,
        failed=bucket.failed,
        reviewed=bucket.reviewed,
        not_applicable=bucket.not_applicable,
        errors=bucket.errors,
        applicable=bucket.applicable,
        pass_rate=bucket.pass_rate,
        failure_rate=bucket.failure_rate,
        average_score=bucket.average_score,
    )


def compare_result_breakdowns(
    baseline: ResultBreakdown,
    candidate: ResultBreakdown,
) -> tuple[ComparisonSliceDelta, ...]:
    """Align two tag or failure-type breakdowns and return stable deltas."""

    if baseline.dimension != candidate.dimension:
        raise ValueError("comparison breakdowns must use the same dimension")
    if baseline.dimension not in ("tag", "failure_type"):
        raise ValueError("comparison supports only tag and failure_type breakdowns")
    baseline_buckets = {bucket.key: bucket for bucket in baseline.buckets}
    candidate_buckets = {bucket.key: bucket for bucket in candidate.buckets}
    baseline_keys = set(baseline_buckets)
    candidate_keys = set(candidate_buckets)
    if baseline.dimension == "tag" and baseline_keys != candidate_keys:
        raise ValueError("comparison tag keys must match")
    keys = sorted(baseline_keys | candidate_keys)
    deltas: list[ComparisonSliceDelta] = []
    for key in keys:
        baseline_bucket = baseline_buckets.get(key)
        candidate_bucket = candidate_buckets.get(key)
        labels = {
            bucket.label
            for bucket in (baseline_bucket, candidate_bucket)
            if bucket is not None
        }
        if len(labels) != 1:
            raise ValueError(f"comparison slice {key!r} has conflicting labels")
        baseline_value = _slice_value(baseline_bucket)
        candidate_value = _slice_value(candidate_bucket)
        deltas.append(
            ComparisonSliceDelta(
                dimension=baseline.dimension,
                key=key,
                label=next(iter(labels)),
                baseline=baseline_value,
                candidate=candidate_value,
                case_count_delta=(
                    candidate_value.case_count - baseline_value.case_count
                ),
                observation_count_delta=(
                    candidate_value.observation_count
                    - baseline_value.observation_count
                ),
                pass_rate_delta=_optional_delta(
                    baseline_value.pass_rate,
                    candidate_value.pass_rate,
                ),
                failure_rate_delta=_optional_delta(
                    baseline_value.failure_rate,
                    candidate_value.failure_rate,
                ),
                average_score_delta=_optional_delta(
                    baseline_value.average_score,
                    candidate_value.average_score,
                ),
            )
        )
    return tuple(deltas)
