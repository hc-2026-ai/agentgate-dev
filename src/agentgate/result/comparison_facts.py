"""Badcase sets and fixed evidence facts for one A/B comparison."""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import Field, field_validator, model_validator

from agentgate.domain import ComparisonFact, DomainModel, EvaluationReport, Outcome, Trace
from agentgate.domain.base import require_non_blank

from .comparison import EvaluationComparison
from .comparison_slices import ComparisonSliceDelta


_FACT_ORDER = (
    "quality.overall_score_delta",
    "performance.case_latency_p95_change_ratio",
    "badcase.new_case_count",
)


class BadcaseComparison(DomainModel):
    """Deduplicated Badcase Case identities on both sides of a comparison."""

    baseline_case_ids: tuple[str, ...]
    candidate_case_ids: tuple[str, ...]
    new_case_ids: tuple[str, ...]
    baseline_case_count: int = Field(ge=0)
    candidate_case_count: int = Field(ge=0)
    new_case_count: int = Field(ge=0)

    @field_validator("baseline_case_ids", "candidate_case_ids", "new_case_ids")
    @classmethod
    def validate_case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(require_non_blank(item, "Badcase Case id") for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("Badcase Case ids must be unique")
        if normalized != tuple(sorted(normalized)):
            raise ValueError("Badcase Case ids must be sorted")
        return normalized

    @model_validator(mode="after")
    def validate_derived_fields(self) -> "BadcaseComparison":
        expected_new = tuple(
            sorted(set(self.candidate_case_ids).difference(self.baseline_case_ids))
        )
        if self.new_case_ids != expected_new:
            raise ValueError("new_case_ids must equal candidate minus baseline Badcases")
        expected_counts = (
            len(self.baseline_case_ids),
            len(self.candidate_case_ids),
            len(self.new_case_ids),
        )
        actual_counts = (
            self.baseline_case_count,
            self.candidate_case_count,
            self.new_case_count,
        )
        if actual_counts != expected_counts:
            raise ValueError("Badcase counts must match their Case ids")
        return self


class RunComparisonAnalysis(DomainModel):
    """Composite read model for comparison, slices, Badcases, and fixed facts."""

    comparison: EvaluationComparison
    tag_deltas: tuple[ComparisonSliceDelta, ...]
    failure_type_deltas: tuple[ComparisonSliceDelta, ...]
    badcases: BadcaseComparison
    facts: tuple[ComparisonFact, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> "RunComparisonAnalysis":
        self._validate_slices(self.tag_deltas, "tag")
        self._validate_slices(self.failure_type_deltas, "failure_type")

        fact_ids = tuple(fact.metric_id for fact in self.facts)
        if fact_ids != _FACT_ORDER:
            raise ValueError("comparison facts must use the fixed metric order")

        comparison_case_ids = set(_comparison_case_ids(self.comparison))
        referenced_badcases = set(self.badcases.baseline_case_ids) | set(
            self.badcases.candidate_case_ids
        )
        if not referenced_badcases.issubset(comparison_case_ids):
            raise ValueError("Badcase ids must reference compared Cases")

        overall = next(
            item
            for item in self.comparison.metric_deltas
            if item.level == "overall" and item.key == "overall"
        )
        quality = self.facts[0]
        if (
            quality.baseline_value,
            quality.candidate_value,
            quality.observed_value,
        ) != (overall.baseline.score, overall.candidate.score, overall.score_delta):
            raise ValueError("quality fact must match the overall Metric delta")

        badcase = self.facts[2]
        if (
            badcase.baseline_value,
            badcase.candidate_value,
            badcase.observed_value,
        ) != (
            float(self.badcases.baseline_case_count),
            float(self.badcases.candidate_case_count),
            float(self.badcases.new_case_count),
        ):
            raise ValueError("Badcase fact must match the Badcase comparison")
        return self

    @staticmethod
    def _validate_slices(
        slices: tuple[ComparisonSliceDelta, ...],
        expected_dimension: str,
    ) -> None:
        if any(item.dimension != expected_dimension for item in slices):
            raise ValueError(f"{expected_dimension} deltas use the wrong dimension")
        keys = tuple(item.key for item in slices)
        if len(set(keys)) != len(keys):
            raise ValueError(f"{expected_dimension} delta keys must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError(f"{expected_dimension} delta keys must be sorted")


def _report_case_ids(report: EvaluationReport) -> tuple[str, ...]:
    return tuple(case.id for case in report.run.manifest.execution_cases)


def _badcase_ids(report: EvaluationReport) -> tuple[str, ...]:
    primary_ids = set(report.run.manifest.primary_evaluator_ids)
    return tuple(
        sorted(
            {
                result.case_id
                for result in report.results
                if result.evaluator_id in primary_ids
                and result.outcome in (Outcome.FAIL, Outcome.ERROR)
            }
        )
    )


def compare_badcases(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
) -> BadcaseComparison:
    """Compare primary FAIL/ERROR Case sets without counting REVIEW as Badcase."""

    if _report_case_ids(baseline) != _report_case_ids(candidate):
        raise ValueError("reports use different ordered Case identities")
    if (
        baseline.run.manifest.primary_evaluator_ids
        != candidate.run.manifest.primary_evaluator_ids
    ):
        raise ValueError("reports use different primary Evaluators")
    baseline_ids = _badcase_ids(baseline)
    candidate_ids = _badcase_ids(candidate)
    new_ids = tuple(sorted(set(candidate_ids).difference(baseline_ids)))
    return BadcaseComparison(
        baseline_case_ids=baseline_ids,
        candidate_case_ids=candidate_ids,
        new_case_ids=new_ids,
        baseline_case_count=len(baseline_ids),
        candidate_case_count=len(candidate_ids),
        new_case_count=len(new_ids),
    )


def _comparison_case_ids(comparison: EvaluationComparison) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.case_id for item in comparison.case_deltas))


def _case_latency_ms(trace: Trace) -> float | None:
    if not trace.spans:
        return None
    started_at = min(span.started_at for span in trace.spans)
    ended_at = max(span.ended_at for span in trace.spans)
    return (ended_at - started_at).total_seconds() * 1000


def _nearest_rank_p95(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("P95 requires at least one latency")
    ordered = sorted(values)
    index = math.ceil(0.95 * len(ordered)) - 1
    return ordered[index]


def _latencies_by_case(
    traces: Sequence[Trace],
    run_id: str,
    expected_case_ids: tuple[str, ...],
) -> dict[str, float]:
    expected = set(expected_case_ids)
    seen: set[str] = set()
    latencies: dict[str, float] = {}
    for trace in traces:
        if trace.run_id != run_id:
            raise ValueError("Trace belongs to a different Run")
        if trace.case_id not in expected:
            raise ValueError("Trace references an unknown comparison Case")
        if trace.case_id in seen:
            raise ValueError("Traces contain a duplicate Case")
        seen.add(trace.case_id)
        latency = _case_latency_ms(trace)
        if latency is not None:
            latencies[trace.case_id] = latency
    return latencies


def _latency_fact(
    comparison: EvaluationComparison,
    baseline_traces: Sequence[Trace],
    candidate_traces: Sequence[Trace],
    expected_case_ids: tuple[str, ...],
) -> ComparisonFact:
    baseline_latencies = _latencies_by_case(
        baseline_traces,
        comparison.baseline_run_id,
        expected_case_ids,
    )
    candidate_latencies = _latencies_by_case(
        candidate_traces,
        comparison.candidate_run_id,
        expected_case_ids,
    )
    expected = set(expected_case_ids)
    baseline_complete = set(baseline_latencies) == expected
    candidate_complete = set(candidate_latencies) == expected
    baseline_p95 = (
        _nearest_rank_p95(tuple(baseline_latencies.values()))
        if baseline_latencies
        else None
    )
    candidate_p95 = (
        _nearest_rank_p95(tuple(candidate_latencies.values()))
        if candidate_latencies
        else None
    )
    if not baseline_complete or not candidate_complete:
        reason = (
            "both_latency_incomplete"
            if not baseline_complete and not candidate_complete
            else "baseline_latency_incomplete"
            if not baseline_complete
            else "candidate_latency_incomplete"
        )
        return ComparisonFact(
            metric_id="performance.case_latency_p95_change_ratio",
            baseline_value=baseline_p95,
            candidate_value=candidate_p95,
            availability="unavailable",
            unavailable_reason=reason,
        )
    if baseline_p95 == 0:
        return ComparisonFact(
            metric_id="performance.case_latency_p95_change_ratio",
            baseline_value=baseline_p95,
            candidate_value=candidate_p95,
            availability="unavailable",
            unavailable_reason="baseline_latency_zero",
        )
    assert baseline_p95 is not None and candidate_p95 is not None
    return ComparisonFact(
        metric_id="performance.case_latency_p95_change_ratio",
        baseline_value=baseline_p95,
        candidate_value=candidate_p95,
        observed_value=(candidate_p95 - baseline_p95) / baseline_p95,
        availability="available",
    )


def derive_comparison_facts(
    comparison: EvaluationComparison,
    badcases: BadcaseComparison,
    baseline_traces: Sequence[Trace],
    candidate_traces: Sequence[Trace],
    expected_case_ids: Sequence[str],
) -> tuple[ComparisonFact, ...]:
    """Derive the three fixed comparison facts in stable metric order."""

    expected_ids = tuple(
        require_non_blank(case_id, "expected comparison Case id")
        for case_id in expected_case_ids
    )
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("expected Case identities must be unique")
    if expected_ids != _comparison_case_ids(comparison):
        raise ValueError("expected Case identities must match the comparison")

    overall = next(
        item
        for item in comparison.metric_deltas
        if item.level == "overall" and item.key == "overall"
    )
    if overall.score_delta is None:
        quality = ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=overall.baseline.score,
            candidate_value=overall.candidate.score,
            availability="unavailable",
            unavailable_reason="overall_score_unavailable",
        )
    else:
        quality = ComparisonFact(
            metric_id="quality.overall_score_delta",
            baseline_value=overall.baseline.score,
            candidate_value=overall.candidate.score,
            observed_value=overall.score_delta,
            availability="available",
        )
    performance = _latency_fact(
        comparison,
        baseline_traces,
        candidate_traces,
        expected_ids,
    )
    badcase = ComparisonFact(
        metric_id="badcase.new_case_count",
        baseline_value=float(badcases.baseline_case_count),
        candidate_value=float(badcases.candidate_case_count),
        observed_value=float(badcases.new_case_count),
        availability="available",
    )
    return quality, performance, badcase


__all__ = [
    "BadcaseComparison",
    "RunComparisonAnalysis",
    "compare_badcases",
    "derive_comparison_facts",
]
