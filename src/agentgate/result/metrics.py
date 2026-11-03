"""Calculate report summaries from persisted Evaluation Results."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import mean

from agentgate.domain import EvaluationResult, MetricPlan, MetricSummary, Outcome

_SUPPORTED_PLAN = ("p1-equal-mean", "1")


def _counts(results: Iterable[EvaluationResult]) -> dict[str, int]:
    items = tuple(results)
    return {
        "passed": sum(item.outcome == Outcome.PASS for item in items),
        "failed": sum(item.outcome == Outcome.FAIL for item in items),
        "reviewed": sum(item.outcome == Outcome.REVIEW for item in items),
        "not_applicable": sum(
            item.outcome == Outcome.NOT_APPLICABLE for item in items
        ),
        "errors": sum(item.outcome == Outcome.ERROR for item in items),
        "applicable": sum(
            item.outcome in (Outcome.PASS, Outcome.FAIL, Outcome.REVIEW)
            for item in items
        ),
        "total": len(items),
    }


def _score(results: Sequence[EvaluationResult]) -> float | None:
    """Average numeric evaluator scores per Case, then weight Cases equally."""

    by_case: dict[str, list[float]] = defaultdict(list)
    for result in results:
        if result.score is not None:
            by_case[result.case_id].append(result.score)
    case_scores = tuple(mean(scores) for scores in by_case.values())
    return mean(case_scores) if case_scores else None


def _summaries_by_metric(
    results: Sequence[EvaluationResult],
) -> tuple[MetricSummary, ...]:
    grouped: dict[str, list[EvaluationResult]] = defaultdict(list)
    for result in results:
        grouped[result.metric].append(result)
    return tuple(
        MetricSummary(
            key=key,
            level="metric",
            score=_score(items),
            **_counts(items),
        )
        for key, items in grouped.items()
    )


def _metric_dimensions(
    results: Sequence[EvaluationResult],
) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for result in results:
        previous = dimensions.setdefault(result.metric, result.dimension)
        if previous != result.dimension:
            raise ValueError(
                f"metric {result.metric!r} belongs to multiple dimensions: "
                f"{previous!r}, {result.dimension!r}"
            )
    return dimensions


def calculate_metrics(
    results: Sequence[EvaluationResult],
    primary_evaluator_ids: tuple[str, ...],
    plan: MetricPlan,
) -> tuple[MetricSummary, ...]:
    """Apply one supported MetricPlan to primary Evaluation Results."""

    if (plan.id, plan.version) != _SUPPORTED_PLAN:
        raise ValueError(f"unsupported MetricPlan: {plan.id}@{plan.version}")

    primary_ids = set(primary_evaluator_ids)
    primary = tuple(item for item in results if item.evaluator_id in primary_ids)
    metric_summaries = _summaries_by_metric(primary)
    metric_dimensions = _metric_dimensions(primary)

    dimensions: list[MetricSummary] = []
    for dimension in dict.fromkeys(metric_dimensions.values()):
        children = tuple(
            item
            for item in metric_summaries
            if metric_dimensions[item.key] == dimension
        )
        scores = tuple(item.score for item in children if item.score is not None)
        related = tuple(item for item in primary if item.dimension == dimension)
        dimensions.append(MetricSummary(
            key=dimension,
            level="dimension",
            score=mean(scores) if scores else None,
            **_counts(related),
        ))

    kinds: list[MetricSummary] = []
    for kind in dict.fromkeys(item.evaluator_kind.value for item in primary):
        related = tuple(
            item for item in primary if item.evaluator_kind.value == kind
        )
        children = _summaries_by_metric(related)
        scores = tuple(item.score for item in children if item.score is not None)
        kinds.append(MetricSummary(
            key=kind,
            level="kind",
            score=mean(scores) if scores else None,
            **_counts(related),
        ))

    overall = MetricSummary(
        key="overall",
        level="overall",
        score=_score(primary),
        **_counts(primary),
    )
    return (overall, *kinds, *dimensions, *metric_summaries)
