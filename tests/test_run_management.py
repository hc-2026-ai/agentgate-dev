from __future__ import annotations

from datetime import timedelta

import pytest

from agentgate.application import RunManagement, TargetCatalog
from agentgate.application.evaluator_management import (
    EvaluatorCatalogConflict,
    EvaluatorManagement,
    build_default_evaluator_management,
)
from agentgate.demo.bootstrap import (
    ensure_demo_dataset,
    ensure_demo_target_descriptors,
)
from agentgate.demo.loan import LOAN_DATASET, LOAN_DATASET_VERSION
from agentgate.demo.targets import (
    build_demo_target_snapshot,
    get_demo_target_descriptor,
)
from agentgate.domain import (
    Case,
    CaseTurn,
    EvaluatorKind,
    EvaluatorRef,
    EvaluatorSeverity,
    RunStatus,
    TargetSnapshot,
    transition_run,
    utcnow,
)
from agentgate.evaluator.models import DuplicateEvaluatorId, UnknownEvaluator
from agentgate.integrations.observability import InMemoryTraceCapture
from agentgate.integrations.targets import DemoLoanTargetAdapter
from agentgate.storage.sqlite import SQLiteRepository


class RecordingDispatcher:
    def __init__(
        self,
        failure: Exception | None = None,
        cancel_failure: Exception | None = None,
    ) -> None:
        self.failure = failure
        self.cancel_failure = cancel_failure
        self.run_ids: list[str] = []
        self.cancelled_run_ids: list[str] = []

    def submit(self, run_id: str) -> None:
        self.run_ids.append(run_id)
        if self.failure is not None:
            raise self.failure

    def cancel(self, run_id: str) -> None:
        self.cancelled_run_ids.append(run_id)
        if self.cancel_failure is not None:
            raise self.cancel_failure


def target(version: str = "loan-agent-v2-fixed") -> TargetSnapshot:
    return build_demo_target_snapshot(
        get_demo_target_descriptor(version)
    )


def seed_demo(repository: SQLiteRepository) -> None:
    ensure_demo_dataset(repository)
    ensure_demo_target_descriptors(TargetCatalog(repository))


def run_management(
    repository: SQLiteRepository,
) -> tuple[RunManagement, EvaluatorManagement]:
    evaluators = build_default_evaluator_management(repository)
    return RunManagement(repository, evaluators), evaluators


def test_create_run_persists_exact_pending_manifest(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "create-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)

    run = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        dataset_version=1,
        evaluator_ids=("skill-routing", "final-state"),
        timeout_seconds=30,
    )

    assert run.status is RunStatus.PENDING
    assert repository.get_run(run.id) == run
    assert [spec.id for spec in run.manifest.evaluator_specs] == [
        "skill-routing", "final-state"
    ]
    assert run.manifest.target.ref.external_version_id == "loan-agent-v2-fixed"
    assert run.manifest.timeout_seconds == 30
    assert run.manifest.max_parallel_cases == 1


def test_create_run_persists_case_concurrency_limit(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "create-parallel-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)

    run = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        max_parallel_cases=4,
    )

    assert run.manifest.max_parallel_cases == 4
    assert repository.get_run(run.id) == run

    with pytest.raises(ValueError, match="max_parallel_cases"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            max_parallel_cases=0,
        )


def test_create_run_persists_retry_limit(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "create-retry-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)

    run = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        max_retries=3,
    )

    assert run.manifest.max_retries == 3
    assert repository.get_run(run.id) == run

    with pytest.raises(ValueError, match="max_retries"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            max_retries=-1,
        )


def test_create_rerun_preserves_exact_manifest_and_source(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "create-rerun.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    pending = management.create_run(
        target("loan-agent-v1-risky"),
        dataset_id=LOAN_DATASET.id,
        dataset_version=1,
        case_ids=(LOAN_DATASET_VERSION.cases[0].id,),
        evaluator_ids=("skill-routing", "final-state"),
        timeout_seconds=45,
        max_parallel_cases=2,
        max_retries=3,
    )
    running = repository.claim_pending_run(pending.id, pending.created_at)
    assert running is not None
    completed = transition_run(running, RunStatus.COMPLETED)
    repository.save_run(completed)
    source_payload = repository.get_run(completed.id).model_dump_json()

    rerun = management.create_rerun(completed.id)

    assert rerun.id != completed.id
    assert rerun.status is RunStatus.PENDING
    assert rerun.started_at is None
    assert rerun.completed_at is None
    assert rerun.error is None
    assert rerun.manifest == completed.manifest
    assert rerun.manifest.manifest_sha256 == completed.manifest.manifest_sha256
    assert repository.get_run(rerun.id) == rerun
    assert repository.get_run(completed.id).model_dump_json() == source_payload
    assert repository.list_traces(rerun.id) == []
    assert repository.list_results(rerun.id) == []


def test_create_rerun_accepts_failed_and_cancelled_sources(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "terminal-reruns.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    failed_pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    cancelled_pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    failed_running = repository.claim_pending_run(
        failed_pending.id,
        failed_pending.created_at,
    )
    assert failed_running is not None
    failed = transition_run(failed_running, RunStatus.FAILED, error="Target failed")
    repository.save_run(failed)
    cancelled = repository.cancel_run(
        cancelled_pending.id,
        cancelled_pending.created_at, user_team_id="")
    assert cancelled is not None

    failed_rerun = management.create_rerun(failed.id)
    cancelled_rerun = management.create_rerun(cancelled.id)

    assert failed_rerun.status is RunStatus.PENDING
    assert failed_rerun.manifest == failed.manifest
    assert cancelled_rerun.status is RunStatus.PENDING
    assert cancelled_rerun.manifest == cancelled.manifest


def test_create_rerun_rejects_unknown_and_active_sources(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "invalid-reruns.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    running_pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    running = repository.claim_pending_run(
        running_pending.id,
        running_pending.created_at,
    )
    assert running is not None

    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        management.create_rerun("missing")
    with pytest.raises(ValueError, match="cannot rerun pending"):
        management.create_rerun(pending.id)
    with pytest.raises(ValueError, match="cannot rerun running"):
        management.create_rerun(running.id)

    assert {run.id for run in repository.list_runs(user_team_id="")} == {pending.id, running.id}


def test_create_run_snapshots_latest_enabled_user_evaluator_version(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "user-evaluator-run.db")
    seed_demo(repository)
    runs, evaluators = run_management(repository)
    evaluator, _ = evaluators.create_evaluator(
        "Custom output",
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="custom_output_v1",
        implementation_id="final_output",
        config={},
    )

    with pytest.raises(EvaluatorCatalogConflict, match="disabled"):
        runs.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            evaluator_ids=(evaluator.id,),
        )

    first = evaluators.publish_draft(evaluator.id)
    with pytest.raises(EvaluatorCatalogConflict, match="disabled"):
        runs.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            evaluator_ids=(evaluator.id,),
        )

    evaluators.update_evaluator(evaluator.id, enabled=True)
    first_run = runs.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        evaluator_ids=(evaluator.id,),
    )
    assert first_run.manifest.evaluator_specs == (first,)

    evaluators.create_draft(evaluator.id)
    evaluators.replace_draft(
        evaluator.id,
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="custom_output_v2",
        severity=EvaluatorSeverity.BLOCKING,
        implementation_id="final_output",
        implementation_version="1",
        config={},
        children=(),
        combination=None,
    )
    second = evaluators.publish_draft(evaluator.id)
    evaluators.update_evaluator(evaluator.id, enabled=False)

    stored_first_run = repository.get_run(first_run.id)
    assert stored_first_run is not None
    assert stored_first_run.manifest.evaluator_specs == (first,)
    with pytest.raises(EvaluatorCatalogConflict, match="disabled"):
        runs.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            evaluator_ids=(evaluator.id,),
        )

    evaluators.update_evaluator(evaluator.id, enabled=True)
    second_run = runs.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        evaluator_ids=(evaluator.id,),
    )
    assert second.version == "2"
    assert second_run.manifest.evaluator_specs == (second,)


def test_create_run_selects_an_exact_historical_evaluator_version(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "exact-evaluator-run.db")
    seed_demo(repository)
    runs, evaluators = run_management(repository)
    evaluator, _ = evaluators.create_evaluator(
        "Versioned output",
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="versioned_output_v1",
        implementation_id="final_output",
        config={},
    )
    first = evaluators.publish_draft(evaluator.id)
    evaluators.update_evaluator(evaluator.id, enabled=True)
    evaluators.create_draft(evaluator.id)
    evaluators.replace_draft(
        evaluator.id,
        kind=EvaluatorKind.RULE,
        dimension="answer",
        metric="versioned_output_v2",
        severity=EvaluatorSeverity.BLOCKING,
        implementation_id="final_output",
        implementation_version="1",
        config={},
        children=(),
        combination=None,
    )
    second = evaluators.publish_draft(evaluator.id)

    run = runs.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        evaluator_refs=(
            EvaluatorRef(
                evaluator_id=evaluator.id,
                evaluator_version=first.version,
            ),
        ),
    )

    assert second.version == "2"
    assert run.manifest.evaluator_specs == (first,)


def test_execute_run_uses_engine_adapter_and_trace_resolver(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "execute-run.db")
    seed_demo(repository)
    management, evaluators = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    capture = InMemoryTraceCapture()
    adapter = DemoLoanTargetAdapter(capture)

    completed = management.execute_run(run.id, adapter, capture.resolve)

    assert completed.status is RunStatus.COMPLETED
    assert len(repository.list_traces(run.id)) == 1
    results = repository.list_results(run.id)
    assert len(results) == len(evaluators.default_specs)
    assert all(result.outcome.value not in {"fail", "review", "error"}
               for result in results)
    capture.shutdown()


def test_create_run_rejects_unknown_or_duplicate_evaluators(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "invalid-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)

    with pytest.raises(UnknownEvaluator, match="unknown Evaluator: missing"):
        management.create_run(
            target(), dataset_id=LOAN_DATASET.id, evaluator_ids=("missing",)
        )
    with pytest.raises(DuplicateEvaluatorId, match="must be unique"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            evaluator_ids=("final-state", "final-state"),
        )
    with pytest.raises(DuplicateEvaluatorId, match="must be unique"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            evaluator_refs=(
                EvaluatorRef(
                    evaluator_id="final-state",
                    evaluator_version="1",
                ),
                EvaluatorRef(
                    evaluator_id="final-state",
                    evaluator_version="1",
                ),
            ),
        )


def test_execute_run_rejects_unknown_run(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "missing-run.db")
    management, _ = run_management(repository)
    capture = InMemoryTraceCapture()

    with pytest.raises(ValueError, match="unknown EvaluationRun"):
        management.execute_run(
            "missing", DemoLoanTargetAdapter(capture), capture.resolve
        )
    capture.shutdown()


def test_create_run_requires_persisted_matching_target_descriptor(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "missing-target-descriptor.db")
    ensure_demo_dataset(repository)
    management, _ = run_management(repository)
    fixed = target()

    with pytest.raises(LookupError, match="unknown TargetDescriptor"):
        management.create_run(fixed, dataset_id=LOAN_DATASET.id)

    fixed_descriptor = get_demo_target_descriptor("loan-agent-v2-fixed")
    TargetCatalog(repository).register_descriptor(fixed_descriptor)
    risky_ref = get_demo_target_descriptor("loan-agent-v1-risky").ref
    mismatched = TargetSnapshot(
        ref=risky_ref,
        display_name=fixed.display_name,
        adapter_type=fixed.adapter_type,
        adapter_version=fixed.adapter_version,
        descriptor_sha256=fixed.descriptor_sha256,
        invocation_config=fixed.invocation_config,
    )
    with pytest.raises(ValueError, match="does not match TargetSnapshot"):
        management.create_run(mismatched, dataset_id=LOAN_DATASET.id)

    assert repository.list_runs(user_team_id="") == []


def test_dispatch_run_submits_only_the_persisted_run_id(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "dispatch-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    dispatcher = RecordingDispatcher()

    dispatched = management.dispatch_run(run.id, dispatcher)

    assert dispatched == run
    assert dispatcher.run_ids == [run.id]
    assert repository.get_run(run.id).status is RunStatus.PENDING


def test_dispatch_failure_transitions_to_waiting_for_retry(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "dispatch-failure.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    dispatcher = RecordingDispatcher(
        ConnectionError("redis://user:secret@example.invalid")
    )

    result = management.dispatch_run(run.id, dispatcher)

    assert result.status is RunStatus.WAITING
    assert result.dispatch_attempts == 1
    stored = repository.get_run(run.id)
    assert stored.status is RunStatus.WAITING
    assert stored.dispatch_attempts == 1


def test_dispatch_failure_exhausts_retries_to_failed(tmp_path) -> None:
    from agentgate.application.run_scheduling import MAX_DISPATCH_ATTEMPTS

    repository = SQLiteRepository(tmp_path / "dispatch-exhausted.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    dispatcher = RecordingDispatcher(
        ConnectionError("redis://user:secret@example.invalid")
    )

    result = management.dispatch_run(run.id, dispatcher)
    assert result.status is RunStatus.WAITING
    assert result.dispatch_attempts == 1

    for expected_attempts in range(2, MAX_DISPATCH_ATTEMPTS):
        claimed = repository.claim_waiting_run(run.id, run.created_at)
        assert claimed is not None
        assert claimed.status is RunStatus.PENDING
        result = management.dispatch_run(run.id, dispatcher)
        assert result.status is RunStatus.WAITING
        assert result.dispatch_attempts == expected_attempts

    claimed = repository.claim_waiting_run(run.id, run.created_at)
    assert claimed is not None
    with pytest.raises(RuntimeError, match="Run dispatch failed"):
        management.dispatch_run(run.id, dispatcher)

    failed = repository.get_run(run.id)
    assert failed.status is RunStatus.FAILED
    assert "secret" not in failed.error


def test_cancel_run_persists_pending_and_running_cancellation(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "cancel-runs.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    running = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    claimed = repository.claim_pending_run(running.id, running.created_at)
    assert claimed is not None
    dispatcher = RecordingDispatcher()

    cancelled_pending = management.cancel_run(pending.id, dispatcher)
    cancelled_running = management.cancel_run(running.id, dispatcher)

    assert cancelled_pending.status is RunStatus.CANCELLED
    assert cancelled_pending.started_at is None
    assert cancelled_running.status is RunStatus.CANCELLED
    assert cancelled_running.started_at == claimed.started_at
    assert dispatcher.cancelled_run_ids == [pending.id, running.id]
    assert repository.get_run(pending.id) == cancelled_pending
    assert repository.get_run(running.id) == cancelled_running


def test_cancel_run_is_idempotent_and_rejects_unknown_or_terminal_runs(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "cancel-run-states.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    already_cancelled = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    completed_pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    failed_pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    cancelled = repository.cancel_run(
        already_cancelled.id,
        already_cancelled.created_at, user_team_id="")
    assert cancelled is not None
    completed_running = repository.claim_pending_run(
        completed_pending.id,
        completed_pending.created_at,
    )
    failed_running = repository.claim_pending_run(
        failed_pending.id,
        failed_pending.created_at,
    )
    assert completed_running is not None
    assert failed_running is not None
    completed = transition_run(completed_running, RunStatus.COMPLETED)
    failed = transition_run(
        failed_running,
        RunStatus.FAILED,
        error="Target failed",
    )
    repository.save_run(completed)
    repository.save_run(failed)
    dispatcher = RecordingDispatcher()

    assert management.cancel_run(cancelled.id, dispatcher) == cancelled
    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        management.cancel_run("missing", dispatcher)
    with pytest.raises(ValueError, match="cannot cancel completed"):
        management.cancel_run(completed.id, dispatcher)
    with pytest.raises(ValueError, match="cannot cancel failed"):
        management.cancel_run(failed.id, dispatcher)

    assert dispatcher.cancelled_run_ids == []
    assert repository.get_run(completed.id) == completed
    assert repository.get_run(failed.id) == failed


def test_cancel_run_keeps_final_state_when_dispatcher_signal_fails(
    tmp_path,
    caplog,
) -> None:
    repository = SQLiteRepository(tmp_path / "cancel-signal-failure.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    dispatcher = RecordingDispatcher(
        cancel_failure=ConnectionError("redis password=secret")
    )

    cancelled = management.cancel_run(run.id, dispatcher)

    assert cancelled.status is RunStatus.CANCELLED
    assert repository.get_run(run.id) == cancelled
    assert dispatcher.cancelled_run_ids == [run.id]
    assert "ConnectionError" in caplog.text
    assert "secret" not in caplog.text


def test_cancel_run_preserves_concurrently_completed_state(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "cancel-race.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    dispatcher = RecordingDispatcher()

    def complete_instead(run_id, cancelled_at, *, user_team_id=""):
        running = repository.claim_pending_run(run_id, cancelled_at)
        assert running is not None
        completed = transition_run(
            running,
            RunStatus.COMPLETED,
            occurred_at=cancelled_at,
        )
        repository.save_run(completed)
        return None

    monkeypatch.setattr(repository, "cancel_run", complete_instead)

    with pytest.raises(ValueError, match="cannot cancel completed"):
        management.cancel_run(run.id, dispatcher)

    assert repository.get_run(run.id).status is RunStatus.COMPLETED
    assert dispatcher.cancelled_run_ids == []


def test_fail_stale_runs_preserves_active_and_pending_runs(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "stale-runs.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    stale = management.create_run(
        target(), dataset_id=LOAN_DATASET.id, timeout_seconds=10
    )
    active = management.create_run(
        target(), dataset_id=LOAN_DATASET.id, timeout_seconds=10
    )
    pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    now = max(stale.created_at, active.created_at, pending.created_at) + timedelta(
        seconds=100
    )
    repository.claim_pending_run(stale.id, now - timedelta(seconds=20))
    repository.claim_pending_run(active.id, now - timedelta(seconds=5))

    failed = management.fail_stale_runs(now=now, grace_seconds=5)

    assert [run.id for run in failed] == [stale.id]
    assert repository.get_run(stale.id).status is RunStatus.FAILED
    assert repository.get_run(active.id).status is RunStatus.RUNNING
    assert repository.get_run(pending.id).status is RunStatus.PENDING


def test_fail_stale_runs_accounts_for_parallel_case_batches(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "parallel-stale-runs.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    dataset, draft = management.dataset_management.copy_dataset(
        LOAN_DATASET.id,
        "Four cases",
    )
    source_case_id = draft.cases[0].id
    for _ in range(3):
        management.dataset_management.copy_case(dataset.id, source_case_id)
    management.dataset_management.publish_draft(dataset.id)

    sequential = management.create_run(
        target(),
        dataset_id=dataset.id,
        dataset_version=1,
        timeout_seconds=10,
        max_parallel_cases=1,
    )
    parallel = management.create_run(
        target(),
        dataset_id=dataset.id,
        dataset_version=1,
        timeout_seconds=10,
        max_parallel_cases=2,
    )
    now = max(sequential.created_at, parallel.created_at) + timedelta(seconds=100)
    started_at = now - timedelta(seconds=30)
    repository.claim_pending_run(sequential.id, started_at)
    repository.claim_pending_run(parallel.id, started_at)

    failed = management.fail_stale_runs(now=now, grace_seconds=5)

    assert [run.id for run in failed] == [parallel.id]
    assert repository.get_run(sequential.id).status is RunStatus.RUNNING
    assert repository.get_run(parallel.id).status is RunStatus.FAILED


def test_fail_stale_runs_accounts_for_retry_attempts_and_delays(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "retry-stale-runs.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    still_active = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        timeout_seconds=10,
        max_retries=2,
    )
    stale = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        timeout_seconds=10,
        max_retries=2,
    )
    now = max(still_active.created_at, stale.created_at) + timedelta(seconds=100)
    repository.claim_pending_run(
        still_active.id,
        now - timedelta(seconds=37),
    )
    repository.claim_pending_run(
        stale.id,
        now - timedelta(seconds=39),
    )

    failed = management.fail_stale_runs(now=now, grace_seconds=5)

    assert [run.id for run in failed] == [stale.id]
    assert repository.get_run(still_active.id).status is RunStatus.RUNNING
    assert repository.get_run(stale.id).status is RunStatus.FAILED


def test_fail_stale_runs_rejects_negative_grace_period(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "invalid-grace.db")
    management, _ = run_management(repository)

    with pytest.raises(ValueError, match="must not be negative"):
        management.fail_stale_runs(grace_seconds=-1)

def test_create_run_can_freeze_reproducible_case_subset(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "case-subset-run.db")
    seed_demo(repository)
    management, evaluators = run_management(repository)
    dataset = management.dataset_management.create_dataset("Subset Dataset")
    management.dataset_management.create_draft(dataset.id)
    management.dataset_management.save_case(
        dataset.id,
        Case(
            id="first",
            name="First",
            turns=(CaseTurn(id="first-turn", input={"risk": "high"}),),
        ),
    )
    management.dataset_management.save_case(
        dataset.id,
        Case(
            id="second",
            name="Second",
            turns=(CaseTurn(id="second-turn", input={"risk": "low"}),),
        ),
    )
    full_dataset = management.dataset_management.publish_draft(dataset.id)
    selected_ids = ("second",)

    run = management.create_run(
        target(),
        dataset_id=dataset.id,
        dataset_version=1,
        case_ids=selected_ids,
        evaluator_ids=("skill-routing", "final-state"),
    )

    manifest_dataset = run.manifest.dataset
    assert manifest_dataset == full_dataset
    assert run.manifest.selected_case_ids == selected_ids
    assert tuple(case.id for case in run.manifest.execution_cases) == selected_ids
    other_run = management.create_run(
        target(),
        dataset_id=dataset.id,
        dataset_version=1,
        case_ids=("first",),
        evaluator_ids=("skill-routing", "final-state"),
    )
    assert other_run.manifest.dataset == full_dataset
    assert other_run.manifest.manifest_sha256 != run.manifest.manifest_sha256

    capture = InMemoryTraceCapture()
    try:
        completed = management.execute_run(
            run.id,
            DemoLoanTargetAdapter(capture),
            capture.resolve,
        )
    finally:
        capture.shutdown()

    assert completed.status is RunStatus.COMPLETED
    assert len(repository.list_traces(run.id)) == len(selected_ids)
    assert len(repository.list_results(run.id)) == len(selected_ids) * 2
    assert len(evaluators.default_specs) >= 2


def test_create_run_rejects_invalid_case_subset(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "invalid-case-subset-run.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    first_case = management.dataset_management.get_version(LOAN_DATASET.id, 1).cases[0]

    with pytest.raises(ValueError, match="at least one Case id"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            dataset_version=1,
            case_ids=(),
        )

    with pytest.raises(ValueError, match="must not contain duplicates"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            dataset_version=1,
            case_ids=(first_case.id, first_case.id),
        )

    with pytest.raises(ValueError, match="unknown Cases: missing-case"):
        management.create_run(
            target(),
            dataset_id=LOAN_DATASET.id,
            dataset_version=1,
            case_ids=("missing-case",),
        )

    assert repository.list_runs(user_team_id="") == []


def test_created_run_records_pending_time_and_rerun_gets_new_clock(tmp_path):
    repository = SQLiteRepository(tmp_path / "pending-clock.db")
    seed_demo(repository)
    management, _ = run_management(repository)
    run = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    assert run.timing.pending_started_at == run.created_at
    assert repository.get_run(run.id).timing == run.timing
    failed = transition_run(run, RunStatus.FAILED, error="test failure")
    repository.save_run(failed)
    rerun = management.create_rerun(run.id)
    assert rerun.timing.pending_started_at > run.timing.pending_started_at
    scheduled = management.create_run(target(), dataset_id=LOAN_DATASET.id, scheduled_for=utcnow() + timedelta(hours=1))
    assert scheduled.timing.pending_started_at is None
