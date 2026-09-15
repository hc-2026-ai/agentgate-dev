"""Read-only application workflows for Runs, Results, Traces, and overview data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentgate.domain import (
    ComparisonGateDecision,
    ComparisonGateSpec,
    EvaluationReport,
    EvaluationRun,
    RunManifest,
    RunStatus,
    Trace,
)
from agentgate.domain.base import normalize_utc, utcnow
from agentgate.result.analytics import ResultAnalytics, calculate_result_analytics
from agentgate.result.comparison import EvaluationComparison, compare_reports
from agentgate.result.comparison_facts import (
    RunComparisonAnalysis,
    compare_badcases,
    derive_comparison_facts,
)
from agentgate.result.comparison_gate import (
    decide_comparison_gate,
    default_comparison_gate_spec,
)
from agentgate.result.comparison_slices import compare_result_breakdowns
from agentgate.result.report import build_evaluation_report
from agentgate.storage.repository import AgentGateRepository
from agentgate.trace.redaction import redact_trace


class RunProgress(BaseModel):
    """Read projection for one Evaluation Run lifecycle and durable progress."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunStatus
    dataset_id: str
    dataset_version: int
    dataset_name: str
    target_name: str
    target_version: str
    total_cases: int = Field(ge=0)
    completed_cases: int = Field(ge=0)
    progress: float = Field(ge=0, le=1)
    created_at: datetime
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None = Field(default=None, ge=0)
    error: str | None
    queue_position: int | None = Field(default=None, ge=1)


class RunActivity(BaseModel):
    """Queued, running, and recent terminal Run projections."""

    model_config = ConfigDict(frozen=True)

    status_counts: dict[RunStatus, int]
    scheduled: tuple[RunProgress, ...]
    queued: tuple[RunProgress, ...]
    running: tuple[RunProgress, ...]
    recent: tuple[RunProgress, ...]


class ResultReader:
    """Load persisted evaluation outputs without owning their calculations."""

    def __init__(self, repository: AgentGateRepository) -> None:
        self.repository = repository

    def list_runs(
        self, limit: int = 50, status: RunStatus | None = None
    ) -> list[EvaluationRun]:
        if status is not None:
            return self.repository.list_runs_by_status(status, limit=limit)
        return self.repository.list_runs(limit=limit)

    def get_run_manifest(self, run_id: str) -> RunManifest:
        """Return the immutable execution manifest persisted for one Run."""

        return self._get_run(run_id).manifest

    def get_run_progress(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> RunProgress:
        """Return lifecycle timing and Result-derived progress for one Run."""

        run = self._get_run(run_id)
        queue_position = None
        if run.status is RunStatus.PENDING:
            queued_ids = tuple(
                item.id
                for item in self.repository.list_runs_by_status(
                    RunStatus.PENDING, oldest_first=True
                )
            )
            if run.id in queued_ids:
                queue_position = queued_ids.index(run.id) + 1
            else:
                # A worker may claim the Run between these two repository reads.
                run = self._get_run(run_id)
        return self._project_run(run, now=now or utcnow(), queue_position=queue_position)

    def activity(
        self,
        *,
        recent_limit: int = 20,
        now: datetime | None = None,
    ) -> RunActivity:
        """Return uncapped status counts plus active and recent Run projections."""

        if recent_limit < 1:
            raise ValueError("recent Run limit must be at least 1")
        projection_time = normalize_utc(now or utcnow(), "Run activity time")
        scheduled_runs = self.repository.list_runs_by_status(
            RunStatus.SCHEDULED, oldest_first=True
        )
        queued_runs = self.repository.list_runs_by_status(
            RunStatus.PENDING, oldest_first=True
        )
        running_runs = self.repository.list_runs_by_status(
            RunStatus.RUNNING, oldest_first=True
        )
        terminal_runs = sorted(
            (
                run
                for status in (
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                )
                for run in self.repository.list_runs_by_status(status)
            ),
            key=lambda run: (run.completed_at or run.created_at, run.id),
            reverse=True,
        )[:recent_limit]
        return RunActivity(
            status_counts=self.repository.count_runs_by_status(),
            scheduled=tuple(
                self._project_run(run, now=projection_time)
                for run in scheduled_runs
            ),
            queued=tuple(
                self._project_run(run, now=projection_time, queue_position=index)
                for index, run in enumerate(queued_runs, start=1)
            ),
            running=tuple(
                self._project_run(run, now=projection_time)
                for run in running_runs
            ),
            recent=tuple(
                self._project_run(run, now=projection_time)
                for run in terminal_runs
            ),
        )

    def get_report(self, run_id: str) -> EvaluationReport:
        run = self._get_run(run_id)
        if run.status is not RunStatus.COMPLETED:
            raise ValueError("EvaluationReport requires a completed EvaluationRun")
        return build_evaluation_report(run, self.repository.list_results(run.id))

    def get_analytics(self, run_id: str) -> ResultAnalytics:
        run = self._get_run(run_id)
        if run.status is not RunStatus.COMPLETED:
            raise ValueError("Result analytics requires a completed EvaluationRun")
        return calculate_result_analytics(
            run,
            self.repository.list_results(run.id),
        )

    def compare_runs(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> EvaluationComparison:
        """Compare two completed Runs using their persisted reports."""

        return compare_reports(
            self.get_report(baseline_run_id),
            self.get_report(candidate_run_id),
        )

    def analyze_runs(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> RunComparisonAnalysis:
        """Assemble all persisted evidence for one compatible Run comparison."""

        baseline_run = self._get_run(baseline_run_id)
        candidate_run = self._get_run(candidate_run_id)
        if (
            baseline_run.status is not RunStatus.COMPLETED
            or candidate_run.status is not RunStatus.COMPLETED
        ):
            raise ValueError(
                "Run comparison analysis requires completed EvaluationRuns"
            )

        baseline_results = tuple(self.repository.list_results(baseline_run.id))
        candidate_results = tuple(self.repository.list_results(candidate_run.id))
        baseline_report = build_evaluation_report(baseline_run, baseline_results)
        candidate_report = build_evaluation_report(candidate_run, candidate_results)
        comparison = compare_reports(baseline_report, candidate_report)
        baseline_analytics = calculate_result_analytics(
            baseline_run,
            baseline_results,
        )
        candidate_analytics = calculate_result_analytics(
            candidate_run,
            candidate_results,
        )
        badcases = compare_badcases(baseline_report, candidate_report)
        expected_case_ids = tuple(
            case.id for case in baseline_run.manifest.execution_cases
        )
        facts = derive_comparison_facts(
            comparison,
            badcases,
            self.repository.list_traces(baseline_run.id),
            self.repository.list_traces(candidate_run.id),
            expected_case_ids,
        )
        return RunComparisonAnalysis(
            comparison=comparison,
            tag_deltas=compare_result_breakdowns(
                baseline_analytics.by_tag,
                candidate_analytics.by_tag,
            ),
            failure_type_deltas=compare_result_breakdowns(
                baseline_analytics.by_failure_type,
                candidate_analytics.by_failure_type,
            ),
            badcases=badcases,
            facts=facts,
        )

    def get_comparison_gate_defaults(self) -> ComparisonGateSpec:
        """Return the service-owned default A/B comparison rules."""

        return default_comparison_gate_spec()

    def evaluate_comparison_gate(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        spec: ComparisonGateSpec,
    ) -> ComparisonGateDecision:
        """Evaluate submitted rules against facts derived from two Runs."""

        analysis = self.analyze_runs(baseline_run_id, candidate_run_id)
        return decide_comparison_gate(spec, analysis.facts)

    def get_trace(self, run_id: str, case_id: str) -> Trace:
        self._get_run(run_id)
        trace = self.repository.get_trace(run_id, case_id)
        if trace is None:
            raise LookupError(f"unknown Trace: {run_id}/{case_id}")
        return redact_trace(trace)

    def overview(self) -> dict[str, Any]:
        runs = self.repository.list_runs()
        statuses = self.repository.count_runs_by_status()
        datasets = self.repository.list_datasets()
        case_count = 0
        for dataset in datasets:
            version = self.repository.get_latest_published_dataset_version(dataset.id)
            if version is not None:
                case_count += len(version.cases)
        latest_run = next(
            (run for run in runs if run.status is RunStatus.COMPLETED), None
        )
        latest = self.get_report(latest_run.id) if latest_run is not None else None
        return {
            "total_runs": sum(statuses.values()),
            "scheduled_runs": statuses[RunStatus.SCHEDULED],
            "pending_runs": statuses[RunStatus.PENDING],
            "running_runs": statuses[RunStatus.RUNNING],
            "completed_runs": statuses[RunStatus.COMPLETED],
            "failed_runs": statuses[RunStatus.FAILED],
            "cancelled_runs": statuses[RunStatus.CANCELLED],
            "dataset_count": len(datasets),
            "case_count": case_count,
            "latest": latest,
        }

    def _get_run(self, run_id: str) -> EvaluationRun:
        run = self.repository.get_run(run_id)
        if run is None:
            raise LookupError(f"unknown EvaluationRun: {run_id}")
        return run

    def _project_run(
        self,
        run: EvaluationRun,
        *,
        now: datetime,
        queue_position: int | None = None,
    ) -> RunProgress:
        projection_time = normalize_utc(now, "Run progress time")
        expected_evaluators = {spec.id for spec in run.manifest.evaluator_specs}
        results_by_case: dict[str, set[str]] = {}
        for result in self.repository.list_results(run.id):
            results_by_case.setdefault(result.case_id, set()).add(result.evaluator_id)
        completed_cases = sum(
            expected_evaluators.issubset(results_by_case.get(case.id, set()))
            for case in run.manifest.execution_cases
        )
        total_cases = len(run.manifest.execution_cases)
        duration_seconds = None
        if run.started_at is not None:
            duration_end = run.completed_at or projection_time
            duration_seconds = max(
                0.0, (duration_end - run.started_at).total_seconds()
            )
        return RunProgress(
            run_id=run.id,
            status=run.status,
            dataset_id=run.manifest.dataset.dataset_id,
            dataset_version=run.manifest.dataset.version,
            dataset_name=run.manifest.dataset.dataset_name,
            target_name=run.manifest.target.display_name,
            target_version=run.manifest.target.ref.external_version_id,
            total_cases=total_cases,
            completed_cases=completed_cases,
            progress=completed_cases / total_cases if total_cases else 0,
            created_at=run.created_at,
            scheduled_for=run.scheduled_for,
            started_at=run.started_at,
            completed_at=run.completed_at,
            duration_seconds=duration_seconds,
            error=run.error,
            queue_position=queue_position,
        )
