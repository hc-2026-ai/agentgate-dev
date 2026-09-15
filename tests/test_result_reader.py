from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from agentgate.application import ResultReader, RunManagement, TargetCatalog
from agentgate.application.evaluator_management import (
    build_default_evaluator_management,
)
from agentgate.demo.bootstrap import (
    ensure_demo_dataset,
    ensure_demo_target_descriptors,
)
from agentgate.demo.loan import LOAN_DATASET
from agentgate.demo.targets import (
    build_demo_target_snapshot,
    get_demo_target_descriptor,
)
from agentgate.domain import (
    ComparisonGateSpec,
    FrozenJsonObject,
    RunStatus,
    TargetSnapshot,
)
from agentgate.integrations.observability import InMemoryTraceCapture
from agentgate.integrations.targets import DemoLoanTargetAdapter
from agentgate.storage.sqlite import SQLiteRepository


def target() -> TargetSnapshot:
    return build_demo_target_snapshot(
        get_demo_target_descriptor("loan-agent-v2-fixed")
    )


def seed_demo(repository: SQLiteRepository) -> None:
    ensure_demo_dataset(repository)
    ensure_demo_target_descriptors(TargetCatalog(repository))


def run_management(repository: SQLiteRepository) -> RunManagement:
    evaluators = build_default_evaluator_management(repository)
    return RunManagement(repository, evaluators)


def completed_run(repository: SQLiteRepository):
    seed_demo(repository)
    runs = run_management(repository)
    run = runs.create_run(target(), dataset_id=LOAN_DATASET.id)
    capture = InMemoryTraceCapture()
    completed = runs.execute_run(
        run.id, DemoLoanTargetAdapter(capture), capture.resolve
    )
    capture.shutdown()
    return completed


def test_reader_builds_report_and_returns_trace(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "reader.db")
    run = completed_run(repository)
    stored_trace = repository.get_trace(run.id, "high-risk-approval")
    assert stored_trace is not None
    repository.save_trace(
        stored_trace.model_copy(
            update={
                "final_state": FrozenJsonObject(
                    {"status": "human_review", "api_key": "raw-secret"}
                )
            }
        )
    )
    reader = ResultReader(repository)

    report = reader.get_report(run.id)
    trace = reader.get_trace(run.id, "high-risk-approval")
    raw_trace = repository.get_trace(run.id, "high-risk-approval")

    assert report.run == run
    assert report.release_gate.outcome.value == "pass"
    assert trace.run_id == run.id
    assert trace.final_state["api_key"] == "[redacted]"
    assert raw_trace is not None
    assert raw_trace.final_state["api_key"] == "raw-secret"
    assert reader.list_runs() == [run]


def test_reader_rejects_unknown_and_non_completed_runs(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "reader-errors.db")
    seed_demo(repository)
    reader = ResultReader(repository)

    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        reader.get_report("missing")
    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        reader.get_trace("missing", "case")
    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        reader.get_run_manifest("missing")
    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        reader.get_analytics("missing")

    pending = run_management(repository).create_run(
        target(), dataset_id=LOAN_DATASET.id
    )
    with pytest.raises(ValueError, match="completed EvaluationRun"):
        reader.get_report(pending.id)
    with pytest.raises(LookupError, match="unknown Trace"):
        reader.get_trace(pending.id, "missing")
    with pytest.raises(ValueError, match="completed EvaluationRun"):
        reader.get_analytics(pending.id)


def test_reader_calculates_analytics_from_persisted_results(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "reader-analytics.db")
    run = completed_run(repository)

    analytics = ResultReader(repository).get_analytics(run.id)

    assert analytics.run_id == run.id
    assert analytics.by_evaluator.available is True
    assert analytics.by_category.buckets[0].key == "boundary"
    assert analytics.by_difficulty.buckets[0].key == "hard"


def test_reader_returns_exact_manifest_for_pending_and_completed_runs(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "manifest-reader.db")
    seed_demo(repository)
    management = run_management(repository)
    pending = management.create_run(
        target(),
        dataset_id=LOAN_DATASET.id,
        timeout_seconds=45,
        max_parallel_cases=3,
    )
    reader = ResultReader(repository)

    assert reader.get_run_manifest(pending.id) == pending.manifest

    capture = InMemoryTraceCapture()
    completed = management.execute_run(
        pending.id,
        DemoLoanTargetAdapter(capture),
        capture.resolve,
    )
    capture.shutdown()

    assert reader.get_run_manifest(completed.id) == pending.manifest


def test_overview_uses_persisted_status_and_dataset_data(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "overview.db")
    completed_run(repository)
    run_management(repository).create_run(
        target(), dataset_id=LOAN_DATASET.id
    )

    overview = ResultReader(repository).overview()

    assert overview["total_runs"] == 2
    assert overview["pending_runs"] == 1
    assert overview["completed_runs"] == 1
    assert overview["dataset_count"] == 1
    assert overview["case_count"] == 1
    assert overview["latest"].release_gate.outcome.value == "pass"


def test_reader_derives_complete_case_progress_from_result_sets(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "progress.db")
    source = completed_run(repository)
    management = run_management(repository)
    pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    running = repository.claim_pending_run(
        pending.id, pending.created_at + timedelta(seconds=2)
    )
    assert running is not None
    source_trace = repository.get_trace(source.id, "high-risk-approval")
    assert source_trace is not None
    trace_id = uuid4().hex
    trace_payload = source_trace.model_dump(mode="json")
    trace_payload.update({"trace_id": trace_id, "run_id": running.id})
    trace_payload["spans"] = [
        {**span, "trace_id": trace_id}
        for span in trace_payload["spans"]
    ]
    repository.save_trace(type(source_trace).model_validate(trace_payload))
    source_results = repository.list_results(source.id)

    def copy_result(index: int):
        payload = source_results[index].model_dump(mode="json")
        payload.update(
            {"id": str(uuid4()), "run_id": running.id, "trace_id": trace_id}
        )
        return type(source_results[index]).model_validate(payload)

    repository.save_results([copy_result(0)])
    reader = ResultReader(repository)

    partial = reader.get_run_progress(
        running.id, now=running.started_at + timedelta(seconds=3)
    )

    assert partial.status is RunStatus.RUNNING
    assert partial.total_cases == 1
    assert partial.completed_cases == 0
    assert partial.progress == 0
    assert partial.duration_seconds == 3

    repository.save_results(
        [copy_result(index) for index in range(1, len(source_results))]
    )
    complete = reader.get_run_progress(running.id)

    assert complete.completed_cases == 1
    assert complete.progress == 1


def test_reader_projects_queue_activity_and_terminal_history(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "activity.db")
    terminal = completed_run(repository)
    management = run_management(repository)
    first = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    second = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    running = repository.claim_pending_run(
        second.id, second.created_at + timedelta(seconds=1)
    )
    assert running is not None
    now = running.started_at + timedelta(seconds=4)

    reader = ResultReader(repository)
    activity = reader.activity(now=now)

    assert activity.status_counts[RunStatus.PENDING] == 1
    assert activity.status_counts[RunStatus.RUNNING] == 1
    assert activity.status_counts[RunStatus.COMPLETED] == 1
    assert [item.run_id for item in activity.queued] == [first.id]
    assert activity.queued[0].queue_position == 1
    assert [item.run_id for item in activity.running] == [running.id]
    assert activity.running[0].duration_seconds == 4
    assert [item.run_id for item in activity.recent] == [terminal.id]
    assert reader.get_run_progress(first.id).queue_position == 1
    assert reader.list_runs(status=RunStatus.RUNNING) == [running]


def test_progress_tolerates_worker_claim_during_queue_lookup(
    tmp_path, monkeypatch
) -> None:
    repository = SQLiteRepository(tmp_path / "claim-race.db")
    seed_demo(repository)
    management = run_management(repository)
    pending = management.create_run(target(), dataset_id=LOAN_DATASET.id)
    original_list = repository.list_runs_by_status

    def claim_then_list(status, *, limit=None, oldest_first=False):
        if status is RunStatus.PENDING:
            repository.claim_pending_run(
                pending.id, pending.created_at + timedelta(seconds=1)
            )
        return original_list(status, limit=limit, oldest_first=oldest_first)

    monkeypatch.setattr(repository, "list_runs_by_status", claim_then_list)

    progress = ResultReader(repository).get_run_progress(pending.id)

    assert progress.status is RunStatus.RUNNING
    assert progress.queue_position is None


def test_overview_counts_all_runs_beyond_history_page(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "uncapped-overview.db")
    seed_demo(repository)
    management = run_management(repository)
    for _ in range(51):
        management.create_run(target(), dataset_id=LOAN_DATASET.id)

    overview = ResultReader(repository).overview()

    assert overview["total_runs"] == 51
    assert overview["pending_runs"] == 51


def test_reader_assembles_complete_comparison_analysis_without_n_plus_one(
    tmp_path,
    execute_demo,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "comparison-analysis.db")
    baseline, _ = execute_demo(repository, "loan-agent-v1-risky")
    candidate, _ = execute_demo(repository, "loan-agent-v2-fixed")
    original_list_results = repository.list_results
    original_list_traces = repository.list_traces
    calls = {"results": [], "traces": [], "get_trace": []}

    def list_results(run_id):
        calls["results"].append(run_id)
        return original_list_results(run_id)

    def list_traces(run_id):
        calls["traces"].append(run_id)
        return original_list_traces(run_id)

    def get_trace(run_id, case_id):
        calls["get_trace"].append((run_id, case_id))
        raise AssertionError("analysis must not query Traces one Case at a time")

    monkeypatch.setattr(repository, "list_results", list_results)
    monkeypatch.setattr(repository, "list_traces", list_traces)
    monkeypatch.setattr(repository, "get_trace", get_trace)

    analysis = ResultReader(repository).analyze_runs(baseline.id, candidate.id)

    assert analysis.comparison.baseline_run_id == baseline.id
    assert analysis.comparison.candidate_run_id == candidate.id
    assert all(item.dimension == "tag" for item in analysis.tag_deltas)
    assert all(
        item.dimension == "failure_type"
        for item in analysis.failure_type_deltas
    )
    assert tuple(fact.metric_id for fact in analysis.facts) == (
        "quality.overall_score_delta",
        "performance.case_latency_p95_change_ratio",
        "badcase.new_case_count",
    )
    assert analysis.badcases.new_case_count == len(analysis.badcases.new_case_ids)
    assert calls == {
        "results": [baseline.id, candidate.id],
        "traces": [baseline.id, candidate.id],
        "get_trace": [],
    }


def test_reader_returns_defaults_without_accessing_repository(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "comparison-defaults.db")

    def unexpected(*args, **kwargs):
        raise AssertionError("default Gate rules must not access persistence")

    monkeypatch.setattr(repository, "get_run", unexpected)
    monkeypatch.setattr(repository, "list_results", unexpected)
    monkeypatch.setattr(repository, "list_traces", unexpected)

    spec = ResultReader(repository).get_comparison_gate_defaults()

    assert isinstance(spec, ComparisonGateSpec)
    assert len(spec.rules) == 3


def test_reader_evaluates_comparison_gate_from_analysis_facts(
    tmp_path,
    execute_demo,
) -> None:
    repository = SQLiteRepository(tmp_path / "comparison-gate.db")
    baseline, _ = execute_demo(repository, "loan-agent-v1-risky")
    candidate, _ = execute_demo(repository, "loan-agent-v2-fixed")
    reader = ResultReader(repository)
    defaults = reader.get_comparison_gate_defaults()
    quality_only = ComparisonGateSpec(rules=(defaults.rules[0],))

    decision = reader.evaluate_comparison_gate(
        baseline.id,
        candidate.id,
        quality_only,
    )

    assert decision.spec == quality_only
    assert len(decision.conditions) == 1
    assert decision.conditions[0].metric_id == "quality.overall_score_delta"


def test_reader_comparison_analysis_rejects_unknown_and_incomplete_runs(
    tmp_path,
    execute_demo,
) -> None:
    repository = SQLiteRepository(tmp_path / "comparison-errors.db")
    completed, _ = execute_demo(repository, "loan-agent-v2-fixed")
    seed_demo(repository)
    pending = run_management(repository).create_run(
        target(), dataset_id=LOAN_DATASET.id
    )
    reader = ResultReader(repository)

    with pytest.raises(LookupError, match="unknown EvaluationRun"):
        reader.analyze_runs("missing", completed.id)
    with pytest.raises(ValueError, match="completed EvaluationRuns"):
        reader.analyze_runs(pending.id, completed.id)


def test_reader_comparison_analysis_preserves_compatibility_errors(
    tmp_path,
    execute_demo,
    monkeypatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "comparison-incompatible.db")
    baseline, _ = execute_demo(repository, "loan-agent-v1-risky")
    candidate, _ = execute_demo(repository, "loan-agent-v2-fixed")
    original_get_run = repository.get_run
    candidate_ref = candidate.manifest.target.ref.model_copy(
        update={"external_target_id": "another-agent"}
    )
    candidate_target = candidate.manifest.target.model_copy(
        update={"ref": candidate_ref}
    )
    candidate_manifest = candidate.manifest.model_copy(
        update={"target": candidate_target}
    )
    incompatible = candidate.model_copy(update={"manifest": candidate_manifest})

    def get_run(run_id):
        if run_id == candidate.id:
            return incompatible
        return original_get_run(run_id)

    monkeypatch.setattr(repository, "get_run", get_run)

    with pytest.raises(ValueError, match="different Agent or Skill targets"):
        ResultReader(repository).analyze_runs(baseline.id, candidate.id)
