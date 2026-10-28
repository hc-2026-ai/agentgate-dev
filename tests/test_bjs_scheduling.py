"""Both scheduler entry points honor BJS routing and the shared database."""

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from test_bjs_execution import prepare_script_tree, run_script
from test_run_scheduling import create_scheduled_run

from agentgate.application import run_scheduling
from agentgate.domain import RunStatus, utcnow
from agentgate.integrations.job_dispatchers.bjs_job_dispatcher import BjsJobDispatcher
from agentgate.integrations.job_dispatchers.celery import dispatch_due_evaluation_runs
from agentgate.storage.sqlite import SQLiteRepository


def due_run(repository):
    from test_run_engine import pending_run

    template = pending_run()
    now = utcnow()
    run = type(template).model_validate(
        {
            **template.model_dump(),
            "status": "scheduled",
            "created_at": now - timedelta(minutes=2),
            "scheduled_for": now - timedelta(minutes=1),
        }
    )
    repository.save_run(run)
    return run


@pytest.mark.parametrize("error", [TimeoutError("private credential"), OSError("connection failed")])
def test_bjs_transport_failure_persists_waiting_then_failed(tmp_path, monkeypatch, error):
    repository = SQLiteRepository(tmp_path / "failed-dispatch.db")
    try:
        run = due_run(repository)
        monkeypatch.setattr(run_scheduling, "MAX_DISPATCH_ATTEMPTS", 2)
        calls = []

        def opener(request, **kwargs):
            calls.append(request.full_url)
            raise error

        dispatcher = BjsJobDispatcher("https://bjs.example/submit", "job", opener=opener)
        scheduler = run_scheduling.RunScheduling(repository)
        assert scheduler.dispatch_due_runs(dispatcher) == ()
        waiting = repository.get_run(run.id)
        assert waiting.status is RunStatus.WAITING
        assert waiting.dispatch_attempts == 1
        assert scheduler.dispatch_waiting_runs(dispatcher) == ()
        failed = repository.get_run(run.id)
        assert failed.status is RunStatus.FAILED
        assert "after 2 attempts" in failed.error
        assert "private credential" not in failed.error
        assert len(calls) == 2
        assert scheduler.dispatch_waiting_runs(dispatcher) == ()
        assert len(calls) == 2
    finally:
        repository.close()


def test_celery_scheduler_honors_bjs_configuration(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "scheduled.db")
    run = due_run(repository)
    monkeypatch.setenv("AGENTGATE_DB_TYPE", "sqlite")
    monkeypatch.setenv("AGENTGATE_DB", str(repository.path))
    monkeypatch.setenv("AGENT_TASK_DISPATCHER_TYPE", "bjs")
    monkeypatch.setenv("AGENTGATE_BJS_SUBMIT_URL", "https://bjs.example/submit")
    monkeypatch.setenv("AGENTGATE_BJS_JOB_ID", "ai11")
    submitted = []
    monkeypatch.setattr(BjsJobDispatcher, "submit", lambda self, run_id: submitted.append(run_id))
    assert dispatch_due_evaluation_runs.run() == 1
    assert dispatch_due_evaluation_runs.run() == 0
    assert submitted == [run.id]


@pytest.fixture
def local_bjs():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(parse_qs(urlsplit(self.path).query))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"code": "0", "message": "success"}).encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/submit", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_standalone_scheduler_needs_no_celery_and_submits_once(tmp_path, local_bjs):
    url, requests = local_bjs
    repository = SQLiteRepository(tmp_path / "standalone.db")
    run = due_run(repository)
    future = create_scheduled_run(repository, scheduled_for=utcnow() + timedelta(hours=1))
    root = prepare_script_tree(
        tmp_path,
        {
            "AGENTGATE_DB_TYPE": "sqlite",
            "AGENTGATE_DB": str(repository.path),
            "AGENT_TASK_DISPATCHER_TYPE": "bjs",
            "AGENTGATE_BJS_SUBMIT_URL": url,
            "AGENTGATE_BJS_JOB_ID": "ai11",
        },
    )
    for _ in range(2):
        result = run_script(root, "dispatch-due")
        assert result.returncode == 0, result.stderr
    assert requests == [{"taskId": [run.id], "jobId": ["ai11"]}]
    assert repository.get_run(run.id).status is RunStatus.PENDING
    assert repository.get_run(future.id).status is RunStatus.SCHEDULED


@pytest.mark.parametrize("entry", ["celery", "standalone"])
def test_scheduler_entry_recovers_pending_before_dispatch(tmp_path, monkeypatch, local_bjs, entry):
    from agentgate.domain.run import RunTiming
    from agentgate.domain import transition_run

    url, requests = local_bjs
    repository = SQLiteRepository(tmp_path / "recovery.db")
    expired = due_run(repository)
    claimed, = repository.claim_due_scheduled_runs(utcnow())
    old_clock = utcnow() - timedelta(hours=1)
    repository.save_run(claimed.model_copy(update={"timing": RunTiming(pending_started_at=old_clock)}))
    queued = expired.model_copy(update={"id": "waiting-run"})
    repository.save_run(queued)
    claimed, = repository.claim_due_scheduled_runs(utcnow())
    repository.save_run(transition_run(claimed, RunStatus.WAITING))
    config = {
        "AGENTGATE_DB_TYPE": "sqlite", "AGENTGATE_DB": str(repository.path),
        "AGENT_TASK_DISPATCHER_TYPE": "bjs", "AGENTGATE_BJS_SUBMIT_URL": url,
        "AGENTGATE_BJS_JOB_ID": "ai11", "AGENTGATE_PENDING_TIMEOUT_SECONDS": "60",
        "AGENTGATE_MAX_CONCURRENT_RUNS_PER_API_KEY": "1",
    }
    if entry == "standalone":
        root = prepare_script_tree(tmp_path, config)
        result = run_script(root, "dispatch-due")
        assert result.returncode == 0, result.stderr
    else:
        for name, value in config.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(run_scheduling, "MAX_CONCURRENT_RUNS_PER_API_KEY", 1)
        assert dispatch_due_evaluation_runs.run() == 1
    assert repository.get_run(expired.id).status is RunStatus.FAILED
    assert repository.get_run(queued.id).status is RunStatus.PENDING
    assert requests == [{"taskId": [queued.id], "jobId": ["ai11"]}]
