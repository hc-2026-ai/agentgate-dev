"""Timeout recovery contracts shared by SQLite and MySQL."""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_evaluation_queries import seed
from test_run_scheduling import RecordingDispatcher

from agentgate.application import RunScheduling, run_scheduling
from agentgate.domain import EvaluationRun, RunStatus, transition_run, utcnow
from agentgate.domain.run import RunTiming
from agentgate.storage.evaluation_query_models import EvaluationQuery, TaskSummaryQuery


def pending(repo, team="owner"):
    task, runs = seed(repo, team)
    run = runs[0].model_copy(update={"timing": RunTiming(pending_started_at=utcnow())})
    repo.save_run(run)
    return task, run


def test_pending_timeout_boundary_preserves_task_and_team(repository, monkeypatch):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", "60")
    task, run = pending(repository)
    scheduler = RunScheduling(repository)
    start = run.timing.pending_started_at
    assert scheduler.recover_stale_runs(now=start + timedelta(seconds=59)) == []
    failed, = scheduler.recover_stale_runs(now=start + timedelta(seconds=60))
    assert failed.status is RunStatus.FAILED
    assert failed.completed_at == start + timedelta(seconds=60)
    assert "pending deadline" in failed.error
    assert failed.timing == run.timing
    assert repository.count_active_runs_by_api_key(run.api_key) == 0
    page = repository.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="owner")
    assert page.total == 1 and page.items[0].id == task.id
    assert page.items[0].status == "failed"
    assert repository.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="other").total == 0
    overview = repository.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="owner")
    assert overview.task_count == 1 and overview.scored_run_count == 0
    assert scheduler.recover_stale_runs(now=start + timedelta(days=1)) == []


def test_legacy_pending_receives_one_full_grace_period(repository, monkeypatch):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", "60")
    _, runs = seed(repository, "owner")
    old = runs[0]
    # Missing timing is a supported pre-upgrade payload, not a new pending clock.
    raw = old.model_dump(mode="json", exclude={"timing"})
    assert EvaluationRun.model_validate(raw).timing.pending_started_at is None
    now = utcnow()
    scheduler = RunScheduling(repository)
    assert scheduler.recover_stale_runs(now=now) == []
    initialized = repository.get_run(old.id)
    assert initialized.timing.pending_started_at == now
    assert scheduler.recover_stale_runs(now=now + timedelta(seconds=59)) == []
    assert repository.get_run(old.id).timing.pending_started_at == now
    assert len(scheduler.recover_stale_runs(now=now + timedelta(seconds=60))) == 1


@pytest.mark.parametrize("next_status", ["running", "completed", "cancelled", "waiting"])
def test_scan_never_overwrites_concurrent_status_change(repository, next_status):
    _, run = pending(repository)
    now = utcnow()
    if next_status == "completed":
        changed = transition_run(transition_run(run, RunStatus.RUNNING, occurred_at=now), RunStatus.COMPLETED, occurred_at=now)
    else:
        changed = transition_run(run, RunStatus(next_status), occurred_at=now)
    repository.save_run(changed)
    assert repository.recover_run(run, now=now, error="Expired") is None
    assert repository.get_run(run.id) == changed


def test_pending_reentry_resets_clock_and_invalidates_old_scan(repository):
    _, run = pending(repository)
    waiting = transition_run(run, RunStatus.WAITING)
    repository.save_run(waiting)
    later = utcnow() + timedelta(hours=2)
    claimed = repository.claim_waiting_run(run.id, later)
    assert claimed.timing.pending_started_at == later
    assert repository.recover_run(run, now=later, error="Expired") is None
    repository.save_run(claimed)
    assert repository.get_run(run.id).timing.pending_started_at == later


def test_scheduled_claim_sets_pending_clock(repository):
    _, template = pending(repository)
    due = utcnow() + timedelta(hours=1)
    scheduled = template.model_copy(update={"id": str(uuid4()), "status": RunStatus.SCHEDULED, "scheduled_for": due, "timing": RunTiming()})
    repository.save_run(scheduled)
    claimed, = repository.claim_due_scheduled_runs(due)
    assert claimed.timing.pending_started_at == due


def test_duplicate_recovery_and_late_completion_are_rejected(repository):
    _, run = pending(repository)
    running = repository.claim_pending_run(run.id, utcnow())
    now = utcnow()
    failed = repository.recover_run(running, now=now, error="Execution timed out")
    assert failed.status is RunStatus.FAILED
    assert repository.recover_run(running, now=now, error="Execution timed out") is None
    with pytest.raises(ValueError, match="terminal"):
        repository.save_run(transition_run(running, RunStatus.COMPLETED, occurred_at=now))
    assert repository.get_run(run.id) == failed


def test_cancel_failure_does_not_block_other_recovery_or_waiting_dispatch(repository, monkeypatch, caplog):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", "60")
    monkeypatch.setattr(run_scheduling, "MAX_CONCURRENT_RUNS_PER_API_KEY", 1)
    _, first = pending(repository)
    _, second = pending(repository)
    _, queued = pending(repository)
    repository.save_run(transition_run(queued, RunStatus.WAITING))
    class Dispatcher(RecordingDispatcher):
        def cancel(self, run_id):
            raise RuntimeError("secret-not-to-log")
    dispatcher = Dispatcher()
    scheduler = RunScheduling(repository)
    recovered = scheduler.recover_stale_runs(dispatcher, now=utcnow() + timedelta(minutes=2))
    assert {run.id for run in recovered} == {first.id, second.id}
    assert "cancellation failed" in caplog.text and "secret-not-to-log" not in caplog.text
    dispatched, = scheduler.dispatch_waiting_runs(dispatcher)
    assert dispatched.id == queued.id
    assert repository.count_active_runs_by_api_key(queued.api_key) == 1


def test_running_budget_and_nonactive_runs_are_preserved(repository, monkeypatch):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", "1")
    _, run = pending(repository)
    start = utcnow()
    running = repository.claim_pending_run(run.id, start)
    _, waiting = pending(repository)
    repository.save_run(transition_run(waiting, RunStatus.WAITING))
    scheduler = RunScheduling(repository)
    assert scheduler.recover_stale_runs(now=start + timedelta(seconds=1)) == []
    failed, = scheduler.recover_stale_runs(now=start + timedelta(days=10))
    assert failed.id == running.id and "recovery deadline" in failed.error
    assert repository.get_run(waiting.id).status is RunStatus.WAITING


def test_initialization_cannot_reset_existing_clock_or_running_run(repository):
    _, run = pending(repository)
    assert repository.recover_run(run, now=utcnow()) is None
    running = repository.claim_pending_run(run.id, utcnow())
    assert repository.recover_run(running, now=utcnow()) is None
    missing = run.model_copy(update={"id": str(uuid4())})
    assert repository.recover_run(missing, now=utcnow()) is None


def test_scan_skips_task_claimed_after_read(repository, monkeypatch):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", "1")
    _, run = pending(repository)
    original = repository.recover_run
    def claim_before_recovery(expected, **kwargs):
        repository.claim_pending_run(expected.id, utcnow())
        return original(expected, **kwargs)
    monkeypatch.setattr(repository, "recover_run", claim_before_recovery)
    assert RunScheduling(repository).recover_stale_runs(now=utcnow() + timedelta(seconds=2)) == []
    assert repository.get_run(run.id).status is RunStatus.RUNNING


@pytest.mark.parametrize("value", ["0", "bad", "-1"])
def test_invalid_pending_configuration_uses_default(repository, monkeypatch, value):
    monkeypatch.setenv("AGENTGATE_PENDING_TIMEOUT_SECONDS", value)
    _, run = pending(repository)
    scheduler = RunScheduling(repository)
    assert scheduler.recover_stale_runs(now=run.timing.pending_started_at + timedelta(seconds=1799)) == []
    assert len(scheduler.recover_stale_runs(now=run.timing.pending_started_at + timedelta(seconds=1800))) == 1


def test_two_reapers_only_release_a_run_once(repository):
    from concurrent.futures import ThreadPoolExecutor

    _, run = pending(repository)
    now = utcnow()
    def recover(_):
        return repository.recover_run(run, now=now, error="Execution timed out")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(recover, range(2)))
    assert sum(value is not None for value in outcomes) == 1
    assert repository.get_run(run.id).status is RunStatus.FAILED
    assert repository.count_active_runs_by_api_key(run.api_key) == 0
