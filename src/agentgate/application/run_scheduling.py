"""Application workflow for releasing due Evaluation Runs."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from agentgate.domain import EvaluationRun, RunStatus, normalize_utc, transition_run, utcnow
from agentgate.integrations.job_dispatchers import JobDispatcher
from agentgate.run.retry import retry_delay_seconds
from agentgate.storage.repository import AgentGateRepository

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_RUNS_PER_API_KEY = 10
DEFAULT_MAX_DISPATCH_ATTEMPTS = 10


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("env %s=%r is not an integer, using default %d", name, raw, default)
        return default
    if value < 1:
        LOGGER.warning("env %s=%d is below 1, using default %d", name, value, default)
        return default
    return value


MAX_CONCURRENT_RUNS_PER_API_KEY = _positive_int_env(
    "AGENTGATE_MAX_CONCURRENT_RUNS_PER_API_KEY",
    DEFAULT_MAX_CONCURRENT_RUNS_PER_API_KEY,
)
MAX_DISPATCH_ATTEMPTS = _positive_int_env(
    "AGENTGATE_MAX_DISPATCH_ATTEMPTS",
    DEFAULT_MAX_DISPATCH_ATTEMPTS,
)


def _transition_to_waiting(run: EvaluationRun, *, increment_attempts: bool = False) -> EvaluationRun:
    """Transition a Run to WAITING, optionally incrementing dispatch_attempts."""

    waiting = transition_run(run, RunStatus.WAITING)
    if increment_attempts:
        waiting = waiting.model_copy(update={"dispatch_attempts": run.dispatch_attempts + 1})
    return waiting


def _handle_dispatch_failure(
    run: EvaluationRun,
    exc: Exception,
    *,
    occurred_at: datetime,
    repository: AgentGateRepository,
) -> EvaluationRun:
    """Persist a failed dispatch as WAITING (retry) or FAILED (attempts exhausted)."""

    attempts = run.dispatch_attempts + 1
    if attempts < MAX_DISPATCH_ATTEMPTS:
        waiting = transition_run(run, RunStatus.WAITING)
        waiting = waiting.model_copy(update={"dispatch_attempts": attempts})
        repository.save_run(waiting)
        LOGGER.warning(
            "Run dispatch failed (attempt %d/%d): run_id=%s, %s",
            attempts, MAX_DISPATCH_ATTEMPTS, run.id, type(exc).__name__,
        )
        return waiting
    failed = transition_run(
        run,
        RunStatus.FAILED,
        occurred_at=occurred_at,
        error=f"Run dispatch failed after {attempts} attempts: {type(exc).__name__}",
    )
    repository.save_run(failed)
    return failed


class RunScheduling:
    """Move due scheduled Runs into the asynchronous execution queue."""

    def __init__(self, repository: AgentGateRepository) -> None:
        self.repository = repository

    def recover_stale_runs(
        self,
        dispatcher: JobDispatcher | None = None,
        *,
        now: datetime | None = None,
        grace_seconds: float = 30,
    ) -> list[EvaluationRun]:
        """Recover abandoned runs without overwriting concurrent worker progress."""
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        current_time = normalize_utc(now or utcnow(), "stale Run check time")
        pending_timeout = _positive_int_env("AGENTGATE_PENDING_TIMEOUT_SECONDS", 86400)
        recovered: list[EvaluationRun] = []
        LOGGER.debug("Run recovery scan started")
        for status in (RunStatus.PENDING, RunStatus.RUNNING):
            for run in self.repository.list_runs_by_status(status):
                if status is RunStatus.PENDING:
                    started_at = run.timing.pending_started_at
                    if started_at is None:
                        initialized = self.repository.recover_run(run, now=current_time)
                        if initialized is not None:
                            LOGGER.info("Pending recovery clock initialized: run_id=%s", run.id)
                        continue
                    deadline = started_at + timedelta(seconds=pending_timeout)
                    error = "Execution worker did not start before the pending deadline"
                else:
                    manifest = run.manifest
                    cases = len(manifest.execution_cases)
                    batches = (cases + manifest.max_parallel_cases - 1) // manifest.max_parallel_cases
                    retry_budget = sum(
                        retry_delay_seconds(n) for n in range(1, manifest.max_retries + 1)
                    )
                    seconds = manifest.timeout_seconds * batches + cases * (
                        manifest.timeout_seconds * manifest.max_retries + retry_budget
                    )
                    deadline = run.started_at + timedelta(seconds=seconds + grace_seconds)
                    error = "Execution worker exceeded its recovery deadline"
                if deadline > current_time:
                    continue
                failed = self.repository.recover_run(run, now=current_time, error=error)
                if failed is None:
                    LOGGER.debug("Run recovery skipped after concurrent update: run_id=%s", run.id)
                    continue
                recovered.append(failed)
                LOGGER.warning(
                    "Run timed out: run_id=%s previous_status=%s deadline=%s",
                    run.id, status, deadline.isoformat(),
                )
                if dispatcher is not None:
                    try:
                        LOGGER.debug("Cancelling expired execution: run_id=%s", run.id)
                        dispatcher.cancel(run.id)
                    except Exception as exc:  # noqa: BLE001 - cancellation is best effort
                        LOGGER.warning(
                            "Expired execution cancellation failed: run_id=%s error_type=%s",
                            run.id, type(exc).__name__,
                        )
        if recovered:
            LOGGER.info("Run recovery scan completed: recovered_count=%d", len(recovered))
        return recovered

    def dispatch_due_runs(
        self,
        dispatcher: JobDispatcher,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[EvaluationRun, ...]:
        release_time = now or utcnow()
        due_runs = self.repository.claim_due_scheduled_runs(
            release_time,
            limit=limit,
        )
        if due_runs:
            LOGGER.debug("Scheduler: dispatch_due_runs starting, %d due runs", len(due_runs))
        dispatched: list[EvaluationRun] = []
        for run in due_runs:
            active = self.repository.count_active_runs_by_api_key(run.api_key)
            if active > MAX_CONCURRENT_RUNS_PER_API_KEY:
                waiting = _transition_to_waiting(run)
                self.repository.save_run(waiting)
                key_display = "None" if run.api_key is None else "***"
                LOGGER.warning(
                    "Scheduled run throttled to waiting: run_id=%s active_count=%d max=%d api_key=%s",
                    run.id, active, MAX_CONCURRENT_RUNS_PER_API_KEY, key_display,
                )
                continue
            try:
                dispatcher.submit(run.id)
            except Exception as exc:
                _handle_dispatch_failure(
                    run, exc, occurred_at=release_time, repository=self.repository
                )
                continue
            LOGGER.info("Scheduled run dispatched: run_id=%s", run.id)
            dispatched.append(run)
        return tuple(dispatched)

    def dispatch_waiting_runs(
        self,
        dispatcher: JobDispatcher,
        *,
        limit: int = 100,
    ) -> tuple[EvaluationRun, ...]:
        """Dispatch WAITING Runs when concurrency slots become available."""

        waiting_runs = self.repository.list_runs_by_status(
            RunStatus.WAITING, limit=limit, oldest_first=True
        )
        if waiting_runs:
            LOGGER.debug("Scheduler: dispatch_waiting_runs starting, %d waiting runs", len(waiting_runs))
        dispatched: list[EvaluationRun] = []
        for run in waiting_runs:
            active = self.repository.count_active_runs_by_api_key(run.api_key)
            if active >= MAX_CONCURRENT_RUNS_PER_API_KEY:
                key_display = "None" if run.api_key is None else "***"
                LOGGER.debug(
                    "Waiting run still throttled: run_id=%s active_count=%d max=%d api_key=%s",
                    run.id, active, MAX_CONCURRENT_RUNS_PER_API_KEY, key_display,
                )
                continue
            claimed = self.repository.claim_waiting_run(run.id, utcnow())
            if claimed is None:
                continue
            try:
                dispatcher.submit(claimed.id)
            except Exception as exc:
                _handle_dispatch_failure(
                    claimed, exc, occurred_at=utcnow(), repository=self.repository
                )
                continue
            LOGGER.info("Waiting run dispatched: run_id=%s", claimed.id)
            dispatched.append(claimed)
        return tuple(dispatched)
