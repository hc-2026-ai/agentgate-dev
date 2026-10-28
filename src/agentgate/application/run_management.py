"""Application workflows for creating and executing Evaluation Runs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from agentgate.domain import (
    EvaluationRun,
    EvaluatorRef,
    MetricPlan,
    ReleaseGateSpec,
    RunManifest,
    RunStatus,
    TargetSnapshot,
    normalize_utc,
    transition_run,
    utcnow,
)
from agentgate.domain.run import RunTiming
from agentgate.integrations.job_dispatchers import JobDispatcher
from agentgate.run.engine import RunEngine, TraceResolver
from agentgate.run.target_protocol import TargetAdapterProtocol
from agentgate.server.user_context import get_user_info
from agentgate.storage.repository import AgentGateRepository

from .dataset_management import DatasetManagement
from .evaluator_management import EvaluatorManagement
from .run_scheduling import MAX_CONCURRENT_RUNS_PER_API_KEY, MAX_DISPATCH_ATTEMPTS, RunScheduling
from .target_catalog import TargetCatalog

LOGGER = logging.getLogger(__name__)


class RunManagement:
    """Coordinate immutable Run creation and worker-side execution."""

    def __init__(
        self,
        repository: AgentGateRepository,
        evaluator_management: EvaluatorManagement,
    ) -> None:
        self.repository = repository
        self.dataset_management = DatasetManagement(repository)
        self.evaluator_management = evaluator_management
        self.target_catalog = TargetCatalog(repository)

    def create_run(
        self,
        target: TargetSnapshot,
        *,
        dataset_id: str,
        dataset_version: int | None = None,
        case_ids: Sequence[str] | None = None,
        evaluator_ids: Sequence[str] | None = None,
        evaluator_refs: Sequence[EvaluatorRef] | None = None,
        metric_plan: MetricPlan | None = None,
        gate_spec: ReleaseGateSpec | None = None,
        timeout_seconds: float = 300,
        max_parallel_cases: int = 1,
        max_retries: int = 0,
        scheduled_for: datetime | None = None,
        persist: bool = True,
        api_key: str | None = None,
    ) -> EvaluationRun:
        """Resolve exact inputs and persist a pending or scheduled Run."""

        self.target_catalog.resolve_descriptor(
            target.ref,
            target.descriptor_sha256,
        )
        dataset = (
            self.dataset_management.get_version(dataset_id, dataset_version)
            if dataset_version is not None
            else self.dataset_management.latest_published(dataset_id)
        )
        if evaluator_ids is not None and evaluator_refs is not None:
            raise ValueError("use evaluator_ids or evaluator_refs, not both")
        selected = (
            self.evaluator_management.select_versions(evaluator_refs)
            if evaluator_refs is not None
            else self.evaluator_management.select(evaluator_ids)
        )
        self.evaluator_management.validate_plan(dataset, selected)
        if evaluator_refs is not None:
            primary_ids = tuple(ref.evaluator_id for ref in evaluator_refs)
        elif evaluator_ids is not None:
            primary_ids = tuple(evaluator_ids)
        else:
            primary_ids = tuple(spec.id for spec in selected)
        created_at = utcnow()
        normalized_schedule = (
            normalize_utc(scheduled_for, "EvaluationRun scheduled_for")
            if scheduled_for is not None
            else None
        )
        info = get_user_info()
        user_team_id = info.user_team_id if info else ""
        user_id = info.user_id if info else ""
        user_name = info.user_name if info else ""
        run = EvaluationRun(
            manifest=RunManifest(
                dataset=dataset,
                selected_case_ids=tuple(case_ids) if case_ids is not None else None,
                target=target,
                evaluator_specs=selected,
                primary_evaluator_ids=primary_ids,
                metric_plan=metric_plan or MetricPlan(),
                gate_spec=gate_spec or ReleaseGateSpec(),
                timeout_seconds=timeout_seconds,
                max_parallel_cases=max_parallel_cases,
                max_retries=max_retries,
            ),
            status=(
                RunStatus.SCHEDULED
                if normalized_schedule is not None
                else RunStatus.PENDING
            ),
            created_at=created_at,
            timing=RunTiming(pending_started_at=created_at if normalized_schedule is None else None),
            scheduled_for=normalized_schedule,
            user_team_id=user_team_id,
            user_id=user_id,
            user_name=user_name,
            api_key=api_key,
        )
        if persist:
            self.repository.save_run(run)
        key_display = "None" if run.api_key is None else "***"
        LOGGER.info(
            "Run created: run_id=%s status=%s dataset_id=%s case_count=%d user_id=%s api_key=%s",
            run.id,
            run.status.value,
            dataset.dataset_id,
            len(run.manifest.execution_cases),
            user_id,
            key_display,
        )
        return run

    def create_rerun(self, source_run_id: str, *, persist: bool = True) -> EvaluationRun:
        """Create a pending Run from one terminal Run's exact manifest."""

        info = get_user_info()
        user_team_id = info.user_team_id if info else ""
        source = self.repository.get_run(
            source_run_id, user_team_id=user_team_id
        )
        if source is None:
            raise LookupError(f"unknown EvaluationRun: {source_run_id}")
        if source.status in {
            RunStatus.SCHEDULED,
            RunStatus.PENDING,
            RunStatus.WAITING,
            RunStatus.RUNNING,
        }:
            raise ValueError(
                f"cannot rerun {source.status.value} EvaluationRun"
            )

        rerun = EvaluationRun(
            timing=RunTiming(pending_started_at=utcnow()),
            manifest=source.manifest,
            user_team_id=source.user_team_id,
            user_id=source.user_id,
            user_name=source.user_name,
            api_key=source.api_key,
        )
        if persist:
            self.repository.save_run(rerun)
        return rerun

    def execute_run(
        self,
        run_id: str,
        target_adapter: TargetAdapterProtocol,
        trace_resolver: TraceResolver,
    ) -> EvaluationRun:
        """Execute one previously created pending Run through the shared Engine."""

        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown EvaluationRun: {run_id}")
        if run.status is not RunStatus.PENDING:
            return run
        engine = RunEngine(
            self.repository,
            self.evaluator_management.evaluate_case,
            trace_resolver,
        )
        return engine.execute(run, target_adapter)

    def dispatch_run(
        self, run_id: str, dispatcher: JobDispatcher
    ) -> EvaluationRun:
        """Submit one persisted pending Run for worker-side execution."""

        info = get_user_info()
        user_team_id = info.user_team_id if info else ""
        run = self.repository.get_run(run_id, user_team_id=user_team_id)
        if run is None:
            raise ValueError(f"unknown EvaluationRun: {run_id}")
        if run.status is not RunStatus.PENDING:
            raise ValueError("only a pending EvaluationRun can be dispatched")

        active = self.repository.count_active_runs_by_api_key(run.api_key)
        if active > MAX_CONCURRENT_RUNS_PER_API_KEY:
            waiting = transition_run(run, RunStatus.WAITING)
            self.repository.save_run(waiting)
            key_display = "None" if run.api_key is None else "***"
            LOGGER.warning(
                "Run throttled to waiting: run_id=%s active_count=%d max=%d api_key=%s",
                run.id, active, MAX_CONCURRENT_RUNS_PER_API_KEY, key_display,
            )
            return waiting

        try:
            dispatcher.submit(run.id)
        except Exception as exc:
            attempts = run.dispatch_attempts + 1
            if attempts < MAX_DISPATCH_ATTEMPTS:
                waiting = transition_run(run, RunStatus.WAITING)
                waiting = waiting.model_copy(update={"dispatch_attempts": attempts})
                self.repository.save_run(waiting)
                LOGGER.warning(
                    "Run dispatch failed, retrying: run_id=%s attempt=%d/%d error=%s",
                    run.id, attempts, MAX_DISPATCH_ATTEMPTS, type(exc).__name__,
                )
                return waiting
            failed = transition_run(
                run,
                RunStatus.FAILED,
                error=f"Run dispatch failed after {attempts} attempts: {type(exc).__name__}",
            )
            try:
                self.repository.save_run(failed)
            except ValueError:
                current = self.repository.get_run(run.id)
                if current is None or current.status is RunStatus.PENDING:
                    raise
            LOGGER.error(
                "Run dispatch exhausted retries: run_id=%s attempts=%d status=failed",
                run.id, attempts,
            )
            raise RuntimeError("Run dispatch failed") from exc
        LOGGER.info(
            "Run dispatched: run_id=%s active_count=%d max=%d",
            run.id, active, MAX_CONCURRENT_RUNS_PER_API_KEY,
        )
        return run

    def cancel_run(
        self,
        run_id: str,
        dispatcher: JobDispatcher,
    ) -> EvaluationRun:
        """Persist cancellation and best-effort signal its dispatched job."""

        info = get_user_info()
        user_team_id = info.user_team_id if info else ""
        run = self.repository.get_run(run_id, user_team_id=user_team_id)
        if run is None:
            raise LookupError(f"unknown EvaluationRun: {run_id}")
        if run.status is RunStatus.CANCELLED:
            return run
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            raise ValueError(f"cannot cancel {run.status.value} EvaluationRun")
        was_dispatched = run.status in {RunStatus.PENDING, RunStatus.RUNNING}

        cancelled = self.repository.cancel_run(
            run.id, utcnow(), user_team_id=user_team_id
        )
        if cancelled is None:
            current = self.repository.get_run(run.id, user_team_id=user_team_id)
            if current is None:
                raise LookupError(f"unknown EvaluationRun: {run_id}")
            if current.status is RunStatus.CANCELLED:
                return current
            if current.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ValueError(
                    f"cannot cancel {current.status.value} EvaluationRun"
                )
            raise RuntimeError("Run cancellation could not be persisted")

        if not was_dispatched:
            return cancelled
        LOGGER.info(
            "Run cancelled: run_id=%s user_id=%s",
            cancelled.id, info.user_id if info else "",
        )
        try:
            dispatcher.cancel(cancelled.id)
        except Exception as exc:
            LOGGER.warning(
                "Run cancellation signal failed with %s",
                type(exc).__name__,
            )
        return cancelled

    def fail_stale_runs(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float = 30,
    ) -> list[EvaluationRun]:
        """Recover pending and running Runs whose deadlines have expired."""

        return RunScheduling(self.repository).recover_stale_runs(
            now=now, grace_seconds=grace_seconds
        )
