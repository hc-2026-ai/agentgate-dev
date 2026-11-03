from datetime import timedelta
from uuid import uuid4
import pytest
from agentgate.domain import RunStatus, transition_run, utcnow
from agentgate.domain.evaluation_task import EvaluationTask
from agentgate.storage.sqlite import SQLiteRepository
from agentgate.storage.evaluation_query_models import (
    EvaluationQuery,
    TaskSummaryQuery,
    SummarySourceChanged,
)
from agentgate.result.summary import build_completed_run_summary, result_source_digest
from agentgate.application.evaluation_summary_backfill import backfill
from agentgate.storage.evaluation_migration import upgrade_sqlite
from test_evaluation_queries import seed
from test_run_engine import engine, StubTargetAdapter, pending_run


def test_reject_pending_summary_and_invalid_batch(tmp_path):
    with pytest.raises(ValueError):
        build_completed_run_summary(pending_run(), [])
    with pytest.raises(ValueError):
        backfill(None, batch_size=0)
    repo = SQLiteRepository(tmp_path / "q.db")
    with pytest.raises(ValueError):
        repo.list_summary_backfill_ids(entity="bad")
    assert repo.read_run_summary_source("missing") is None
    assert not repo.rebuild_task_query_fields("missing")
    assert not repo.rebuild_dataset_version_query_fields("missing")


def test_completion_rollback_and_idempotency(tmp_path):
    repo = SQLiteRepository(tmp_path / "q.db")
    task, runs = seed(repo, "a")
    completed = engine(repo).execute(runs[0], StubTargetAdapter())
    results = repo.list_results(completed.id)
    summary = build_completed_run_summary(completed, results)
    assert summary.source_results_sha256 == result_source_digest(list(reversed(results)))
    repo.save_completed_run(completed, summary)
    with pytest.raises(SummarySourceChanged):
        repo.save_completed_run(completed, summary.model_copy(update={"result_count": 999}))
    assert (
        repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a").average_score == 1
    )
    with pytest.raises(ValueError):
        build_completed_run_summary(completed, [results[0].model_copy(update={"run_id": "wrong"})])
    repo.save_results(results)
    assert (
        repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a").average_score == 1
    )
    with pytest.raises(ValueError):
        repo.save_completed_run(completed.model_copy(update={"id": "missing"}), summary)
    with pytest.raises(ValueError):
        repo.save_completed_run(completed.model_copy(update={"user_team_id": "b"}), summary)
    with pytest.raises(ValueError):
        repo.save_completed_run(
            completed.model_copy(
                update={"completed_at": completed.completed_at + timedelta(seconds=1)}
            ),
            summary,
        )


def test_old_completion_backfill_and_delete(tmp_path):
    repo = SQLiteRepository(tmp_path / "q.db")
    task, runs = seed(repo, "a")
    running = repo.claim_pending_run(runs[0].id, utcnow())
    completed = transition_run(running, RunStatus.COMPLETED)
    repo.save_run(completed)
    degraded = repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a")
    assert degraded.task_count == 1 and degraded.pending_summary_task_count == 1
    assert backfill(repo, apply=False)["rebuilt"] == 0
    assert not backfill(repo, apply=True)["failures"]
    assert (
        repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a").average_score
        is None
    )
    repo.delete_task_record(task.id, user_team_id="a")
    assert repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="a").total == 0
    with repo._connect() as db:
        assert not db.execute("SELECT * FROM agentgate_run_summaries").fetchall()


def test_old_scoring_summary_is_pending_until_backfill_preserves_evidence(tmp_path):
    from test_run_engine import evaluate_case

    from agentgate.domain import (
        Case,
        CaseTurn,
        EvaluatorSpec,
        FailureStage,
        Outcome,
        ReleaseGateSpec,
        Trace,
    )
    from agentgate.result.report import build_evaluation_report

    repo = SQLiteRepository(tmp_path / "old-scoring.db")
    specs = tuple(
        EvaluatorSpec(
            id=identifier,
            name=identifier,
            dimension=dimension,
            metric=identifier,
            implementation_id="final_output",
        )
        for identifier, dimension in (("one", "tool_use"), ("two", "tool_use"), ("three", "state"))
    )
    cases = tuple(
        Case(id=identifier, name=identifier, turns=(CaseTurn(input={"message": "hello"}),))
        for identifier in ("case-1", "case-2")
    )
    run = pending_run(
        cases=cases,
        evaluator_specs=specs,
        primary_evaluator_ids=tuple(spec.id for spec in specs),
        gate_spec=ReleaseGateSpec(minimum_score=0.8),
    ).model_copy(update={"user_team_id": "history"})
    repo.save_run(run)
    repo.save_evaluation_task(EvaluationTask(kind="single", run_ids=(run.id,)))
    running = repo.claim_pending_run(run.id, utcnow())
    completed = transition_run(running, RunStatus.COMPLETED)
    results = []
    for index, (case, scores) in enumerate(
        zip(cases, ((1.0, 0.0, 1.0), (1.0, None, None)), strict=True), 1
    ):
        trace = Trace(trace_id=f"{index:032x}", run_id=run.id, case_id=case.id, spans=())
        repo.save_trace(trace)
        for spec, score in zip(specs, scores, strict=True):
            item = evaluate_case(case, trace, (spec,))[0]
            if score == 0:
                item = item.model_copy(
                    update={
                        "outcome": Outcome.FAIL,
                        "score": score,
                        "checks": tuple(
                            c.model_copy(
                                update={
                                    "outcome": Outcome.FAIL,
                                    "score": score,
                                    "failure_stage": FailureStage.FINAL_STATE,
                                    "failure_sequence": 0,
                                }
                            )
                            for c in item.checks
                        ),
                        "primary_failure_stage": FailureStage.FINAL_STATE,
                    }
                )
            elif score is None:
                item = item.model_copy(
                    update={
                        "outcome": Outcome.NOT_APPLICABLE,
                        "score": None,
                        "checks": tuple(
                            c.model_copy(update={"outcome": Outcome.NOT_APPLICABLE, "score": None})
                            for c in item.checks
                        ),
                    }
                )
            results.append(item.model_copy(update={"id": f"{case.id}-{spec.id}"}))
    repo.save_results(results)
    repo.save_completed_run(completed, build_completed_run_summary(completed, results))
    with repo._connect() as db:
        db.execute(
            "UPDATE agentgate_run_summaries SET summary_version=1,overall_score=0.75,"
            "gate_reason='score_below_threshold' WHERE run_id=?",
            (run.id,),
        )
        original_run = db.execute(
            "SELECT payload FROM agentgate_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
        original_results = db.execute(
            "SELECT id,payload FROM agentgate_results WHERE run_id=? ORDER BY id", (run.id,)
        ).fetchall()
        original_results = [tuple(row) for row in original_results]

    pending = repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="history")
    assert pending.items[0].runs[0].summary_pending
    assert pending.items[0].runs[0].overall_score is None
    overview = repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="history")
    assert overview.pending_summary_task_count == 1
    assert overview.average_score is None and overview.scored_run_count == 0

    assert not backfill(repo, apply=True)["failures"]
    page = repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="history")
    overview = repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="history")
    report = build_evaluation_report(
        repo.get_run(run.id, user_team_id="history"), repo.list_results(run.id)
    )
    overall = next(metric for metric in report.metrics if metric.level == "overall")
    assert overall.score == pytest.approx(5 / 6)
    assert page.items[0].runs[0].overall_score == overall.score == overview.average_score
    assert page.items[0].runs[0].gate_reason == report.release_gate.reason_code == "threshold_met"
    assert not page.items[0].summary_pending and overview.pending_summary_task_count == 0
    assert overview.scored_run_count == 1
    with repo._connect() as db:
        assert (
            db.execute("SELECT payload FROM agentgate_runs WHERE id=?", (run.id,)).fetchone()[0]
            == original_run
        )
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT id,payload FROM agentgate_results WHERE run_id=? ORDER BY id", (run.id,)
            ).fetchall()
        ] == original_results


def test_rerun_history_and_literal_keyword(tmp_path):
    repo = SQLiteRepository(tmp_path / "q.db")
    task, runs = seed(repo, "a")
    completed = engine(repo).execute(runs[0], StubTargetAdapter())
    replacement = runs[0].model_copy(update={"id": str(uuid4())})
    task = repo.save_task_rerun(task.id, completed.id, replacement, user_team_id="a")
    page = repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="a")
    assert page.total == 1 and page.items[0].runs[0].run_id == replacement.id
    assert repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a").task_count == 1
    repo.save_evaluation_task(task.model_copy(update={"name": "contains %_!"}))
    assert (
        repo.list_evaluation_task_summaries(TaskSummaryQuery(keyword="%_!"), user_team_id="a").total
        == 1
    )
    assert (
        repo.list_evaluation_task_summaries(TaskSummaryQuery(page=4), user_team_id="a").items == ()
    )


@pytest.mark.parametrize(
    "states,expected",
    [
        (["failed", "pending"], "failed"),
        (["cancelled", "running"], "cancelled"),
        (["completed", "pending"], "running"),
        (["completed", "completed"], "completed"),
        (["scheduled", "pending"], "scheduled"),
    ],
)
def test_ab_state_priority(tmp_path, states, expected):
    repo = SQLiteRepository(tmp_path / "q.db")
    task, runs = seed(repo, "a", 2, "ab")
    for run, state in zip(runs, states):
        if state == "scheduled":
            # Query projection fixture, leaving domain lifecycle validation to its tests.
            with repo._connect() as db:
                db.execute("UPDATE agentgate_runs SET status=? WHERE id=?", (state, run.id))
        elif state == "cancelled":
            repo.cancel_run(run.id, utcnow(), user_team_id="a")
        elif state != "pending":
            running = repo.claim_pending_run(run.id, utcnow())
            if state == "completed":
                done = transition_run(running, RunStatus.COMPLETED)
                repo.save_completed_run(done, build_completed_run_summary(done, []))
            elif state == "failed":
                repo.save_run(transition_run(running, RunStatus.FAILED, error="test failure"))
    item = repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id="a").items[0]
    assert item.status == expected
    assert (
        repo.list_evaluation_task_summaries(
            TaskSummaryQuery(status=expected), user_team_id="a"
        ).total
        == 1
    )


def test_backfill_source_conflict_and_retry(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "q.db")
    task, runs = seed(repo, "a")
    source = repo.read_run_summary_source(runs[0].id)
    repo.claim_pending_run(runs[0].id, utcnow())
    with pytest.raises(SummarySourceChanged):
        repo.rebuild_run_summary(source, None)
    attempts = []

    def conflict(*args):
        attempts.append(1)
        raise SummarySourceChanged("changed")

    monkeypatch.setattr(repo, "rebuild_run_summary", conflict)
    report = backfill(repo, apply=True)
    assert len(attempts) == 3 and report["failures"][0]["error_type"] == "SummarySourceChanged"


def test_migration_idempotent_and_type_conflict(tmp_path):
    repo = SQLiteRepository(tmp_path / "q.db")
    with repo._connect() as db:
        upgrade_sqlite(db)
        upgrade_sqlite(db)
        db.execute("ALTER TABLE agentgate_evaluation_tasks DROP COLUMN kind")
        db.execute("ALTER TABLE agentgate_evaluation_tasks ADD COLUMN kind INTEGER")
        with pytest.raises(ValueError):
            upgrade_sqlite(db)


def test_unassociated_team_and_dates(tmp_path):
    repo = SQLiteRepository(tmp_path / "q.db")
    run = pending_run().model_copy(update={"user_team_id": "a"})
    repo.save_run(run)
    q = TaskSummaryQuery(start_at=run.created_at, end_at=run.created_at + timedelta(seconds=1))
    assert repo.list_evaluation_task_summaries(q, user_team_id="a").items[0].task_id is None
    assert (
        repo.get_evaluation_overview_summary(
            EvaluationQuery(end_at=run.created_at), user_team_id="a"
        ).task_count
        == 0
    )
    assert (
        repo.list_evaluation_task_summaries(
            TaskSummaryQuery(start_at=run.created_at + timedelta(days=1)), user_team_id="a"
        ).total
        == 0
    )


def test_engine_public_validation_paths(tmp_path, monkeypatch):
    from agentgate.domain import EvaluationRun
    from agentgate.run.engine import RunEngine
    from test_run_engine import evaluate_case, resolve_trace

    repo = SQLiteRepository(tmp_path / "validation.db")
    run = pending_run()
    repo.save_run(run)
    with pytest.raises(ValueError, match="pending"):
        engine(repo).execute(transition_run(run, RunStatus.RUNNING), StubTargetAdapter())
    with pytest.raises(ValueError, match="does not match execution"):
        engine(repo).execute(run.model_copy(update={"user_id": "other"}), StubTargetAdapter())
    monkeypatch.setattr(repo, "claim_pending_run", lambda *args: None)
    assert engine(repo).execute(run, StubTargetAdapter()).status == RunStatus.PENDING
    calls = []

    def disappear(*args, **kwargs):
        calls.append(1)
        return run if len(calls) == 1 else None

    monkeypatch.setattr(repo, "get_run", disappear)
    with pytest.raises(ValueError, match="disappeared"):
        engine(repo).execute(run, StubTargetAdapter())


def test_engine_wrong_adapter_type(tmp_path):
    repo = SQLiteRepository(tmp_path / "wrong.db")
    run = pending_run()
    repo.save_run(run)
    adapter = StubTargetAdapter()
    adapter.adapter_type = "other"
    with pytest.raises(ValueError, match="adapter_type"):
        engine(repo).execute(run, adapter)
    assert repo.get_run(run.id).status == RunStatus.FAILED


def test_api_error_paths(tmp_path):
    from fastapi.testclient import TestClient
    from agentgate.server.app import create_app

    class Dispatcher:
        def submit(self, run_id):
            pass

    with TestClient(create_app(tmp_path / "api.db", dispatcher=Dispatcher())) as c:
        repo = c.app.state.dependencies.repository
        task, runs = seed(repo, "a")
        with repo._connect() as db:
            db.execute("DELETE FROM agentgate_run_summaries")
        for path in ["/api/evaluation-task-summaries", "/api/evaluation-overview/summary"]:
            assert c.get(path, headers={"X-Team-Id": "a"}).status_code == 200
        assert c.get("/api/evaluation-task-summaries?start_at=2026-01-01").status_code == 422
        assert (
            c.post("/api/evaluation-tasks/x/rerun", json={"source_run_id": "   "}).status_code
            == 422
        )


def test_more_than_200_tasks_and_constant_query_count(tmp_path, monkeypatch):
    from contextlib import contextmanager

    repo = SQLiteRepository(tmp_path / "many.db")
    for _ in range(205):
        seed(repo, "a")
    assert (
        repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id="a").task_count == 205
    )
    observed = []
    connect = repo._connect

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(
                lambda sql: (
                    observed.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
                )
            )
            yield db

    monkeypatch.setattr(repo, "_connect", traced)
    repo.list_evaluation_task_summaries(TaskSummaryQuery(page_size=10), user_team_id="a")
    small = len(observed)
    observed.clear()
    page = repo.list_evaluation_task_summaries(TaskSummaryQuery(page_size=100), user_team_id="a")
    assert len(page.items) == 100 and page.total == 205
    assert len(observed) == small
    last = repo.list_evaluation_task_summaries(
        TaskSummaryQuery(page=3, page_size=100), user_team_id="a"
    )
    assert len(last.items) == 5


def test_mixed_team_ab_is_not_exposed(tmp_path):
    repo = SQLiteRepository(tmp_path / "mixed.db")
    a = pending_run().model_copy(update={"id": "a", "user_team_id": "team-a"})
    b = pending_run().model_copy(update={"id": "b", "user_team_id": "team-b"})
    repo.save_task_runs(EvaluationTask(kind="ab", run_ids=("a", "b")), [a, b])
    for team in ["team-a", "team-b", ""]:
        assert repo.list_evaluation_task_summaries(TaskSummaryQuery(), user_team_id=team).total == 0
        assert (
            repo.get_evaluation_overview_summary(EvaluationQuery(), user_team_id=team).task_count
            == 0
        )
