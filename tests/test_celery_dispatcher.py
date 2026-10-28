from __future__ import annotations

import json

import pytest

from agentgate.application import RunManagement, TargetCatalog
from agentgate.application.evaluator_management import (
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
from agentgate.domain import RunStatus, TargetSnapshot
from agentgate.evaluator.judge import JudgeRequest, JudgeResponse
from agentgate.integrations.job_dispatchers.celery import (
    REDIS_CLUSTER_TRANSPORT,
    CeleryJobDispatcher,
    create_celery_app,
    dispatch_due_evaluation_runs,
    execute_evaluation_run,
)
from agentgate.integrations.model_providers.environment import ConfiguredJudgeModel
from agentgate.storage.sqlite import SQLiteRepository


class RecordingTask:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.app = RecordingApp()

    def apply_async(self, **kwargs) -> None:
        self.calls.append(kwargs)


class RecordingControl:
    def __init__(self) -> None:
        self.revocations: list[tuple[str, bool]] = []

    def revoke(self, task_id: str, *, terminate: bool) -> None:
        self.revocations.append((task_id, terminate))


class RecordingApp:
    def __init__(self) -> None:
        self.control = RecordingControl()


class RecordingJudgeClient:
    provider_id = "company-llm"

    def __init__(self) -> None:
        self.requests: list[JudgeRequest] = []
        self.closed = False

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        self.requests.append(request)
        return JudgeResponse(
            text=json.dumps(
                {
                    "verdict": "pass",
                    "score": 0.9,
                    "confidence": 0.95,
                    "reason": "The answer satisfies the rubric",
                    "violations": [],
                }
            ),
            resolved_model_id="resolved-judge-model",
            request_id="judge-request",
        )

    def close(self) -> None:
        self.closed = True


def configured_judge(client: RecordingJudgeClient) -> ConfiguredJudgeModel:
    return ConfiguredJudgeModel(
        provider_id=client.provider_id,
        model_id="judge-model",
        credential_ref="env:AGENTGATE_JUDGE_API_KEY",
        client=client,  # type: ignore[arg-type]
    )


def target() -> TargetSnapshot:
    return build_demo_target_snapshot(get_demo_target_descriptor("loan-agent-v2-fixed"))


def seed_demo(repository: SQLiteRepository) -> None:
    ensure_demo_dataset(repository)
    ensure_demo_target_descriptors(TargetCatalog(repository))


def test_dispatcher_sends_only_run_id_with_correlated_task_id() -> None:
    task = RecordingTask()
    dispatcher = CeleryJobDispatcher(task=task)  # type: ignore[arg-type]

    dispatcher.submit("run-123")

    assert task.calls == [{"args": ["run-123"], "task_id": "run-123"}]
    with pytest.raises(ValueError, match="run_id must not be blank"):
        dispatcher.submit("  ")


def test_dispatcher_revokes_correlated_task_without_terminating_worker() -> None:
    task = RecordingTask()
    dispatcher = CeleryJobDispatcher(task=task)  # type: ignore[arg-type]

    dispatcher.cancel("run-123")

    assert task.app.control.revocations == [("run-123", False)]
    with pytest.raises(ValueError, match="run_id must not be blank"):
        dispatcher.cancel("  ")
    assert task.app.control.revocations == [("run-123", False)]


def test_celery_app_uses_json_broker_only_configuration(monkeypatch) -> None:
    monkeypatch.setenv("AGENTGATE_REDIS_MODE", "single")
    monkeypatch.setenv("AGENTGATE_REDIS_URL", "redis://broker.example:6379/4")
    monkeypatch.setenv("AGENTGATE_WORKER_CONCURRENCY", "2")
    monkeypatch.setenv("AGENTGATE_TASK_TIME_LIMIT_SECONDS", "420")
    monkeypatch.setenv("AGENTGATE_SCHEDULER_INTERVAL_SECONDS", "17")

    app = create_celery_app()

    assert app.conf.broker_url == "redis://broker.example:6379/4"
    assert app.conf.broker_transport is None
    assert app.conf.broker_transport_options == {}
    assert app.conf.result_backend is None
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_serializer == "json"
    assert app.conf.task_ignore_result is True
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is False
    assert app.conf.worker_concurrency == 2
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 420
    assert app.conf.beat_schedule["dispatch-due-evaluation-runs"]["schedule"] == 17
    assert app.conf.task_routes["agentgate.dispatch_due_evaluation_runs"]["queue"] == (
        "agentgate.scheduler"
    )


def test_celery_app_uses_redis_cluster_transport(monkeypatch) -> None:
    monkeypatch.setenv("AGENTGATE_REDIS_MODE", "cluster")
    monkeypatch.setenv(
        "AGENTGATE_REDIS_URL",
        "redis://user:secret@redis-seed.example:6379/0",
    )
    monkeypatch.setenv("AGENTGATE_REDIS_CLUSTER_HASH_TAG", "{customer-agentgate}")

    app = create_celery_app()

    assert app.conf.broker_url == "redis://user:secret@redis-seed.example:6379/0"
    assert app.conf.broker_transport == REDIS_CLUSTER_TRANSPORT
    assert app.conf.broker_transport_options == {
        "hash_tag": "{customer-agentgate}",
    }
    assert app.conf.result_backend is None


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {"AGENTGATE_REDIS_MODE": "replicated"},
            "AGENTGATE_REDIS_MODE must be single or cluster",
        ),
        (
            {
                "AGENTGATE_REDIS_MODE": "cluster",
                "AGENTGATE_REDIS_URL": "redis://redis-seed.example:6379/2",
            },
            "Redis Cluster supports only database 0",
        ),
        (
            {
                "AGENTGATE_REDIS_MODE": "cluster",
                "AGENTGATE_REDIS_CLUSTER_HASH_TAG": "customer-agentgate",
            },
            "AGENTGATE_REDIS_CLUSTER_HASH_TAG",
        ),
    ],
)
def test_celery_app_rejects_invalid_redis_cluster_configuration(
    monkeypatch, environment, message
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        create_celery_app()


def test_scheduler_task_uses_configured_database_and_dispatcher(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "scheduler-task.db"
    monkeypatch.setenv("AGENTGATE_DB", str(database_path))
    calls: list[str] = []

    class RecordingScheduling:
        def __init__(self, repository) -> None:
            calls.append(repository.path)

        def recover_stale_runs(self, dispatcher):
            calls.append("recover")
            return []

        def dispatch_due_runs(self, dispatcher):
            calls.append(type(dispatcher).__name__)
            return (object(), object())

        def dispatch_waiting_runs(self, dispatcher, *, limit=100):
            return ()

    monkeypatch.setattr(
        "agentgate.integrations.job_dispatchers.celery.RunScheduling",
        RecordingScheduling,
    )

    assert dispatch_due_evaluation_runs.run() == 2
    assert calls == [str(database_path), "recover", "CeleryJobDispatcher"]


def test_worker_executes_persisted_run_and_duplicate_is_noop(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "worker.db"
    monkeypatch.setenv("AGENTGATE_DB", str(database_path))
    repository = SQLiteRepository(database_path)
    seed_demo(repository)
    management = build_default_evaluator_management(repository)
    run = RunManagement(repository, management).create_run(target(), dataset_id=LOAN_DATASET.id)

    assert execute_evaluation_run.run(run.id) == RunStatus.COMPLETED.value
    completed = repository.get_run(run.id)
    assert completed is not None
    assert completed.status is RunStatus.COMPLETED
    result_count = len(repository.list_results(run.id))
    assert result_count == len(management.default_specs)

    assert execute_evaluation_run.run(run.id) == RunStatus.COMPLETED.value
    assert len(repository.list_results(run.id)) == result_count


def test_worker_skips_run_cancelled_before_delivery(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "cancelled-worker.db"
    monkeypatch.setenv("AGENTGATE_DB", str(database_path))
    repository = SQLiteRepository(database_path)
    seed_demo(repository)
    management = build_default_evaluator_management(repository)
    run = RunManagement(repository, management).create_run(target(), dataset_id=LOAN_DATASET.id)
    cancelled = repository.cancel_run(run.id, run.created_at, user_team_id="")
    assert cancelled is not None

    status = execute_evaluation_run.run(run.id)

    assert status == RunStatus.CANCELLED.value
    assert repository.get_run(run.id) == cancelled
    assert repository.list_traces(run.id) == []
    assert repository.list_results(run.id) == []


def test_worker_executes_configured_judge_and_closes_client(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "judge-worker.db"
    monkeypatch.setenv("AGENTGATE_DB", str(database_path))
    repository = SQLiteRepository(database_path)
    seed_demo(repository)
    client = RecordingJudgeClient()
    configuration = configured_judge(client)
    management = build_default_evaluator_management(
        repository,
        judge_client=client,
        judge_model_id=configuration.model_id,
        judge_credential_ref=configuration.credential_ref,
    )
    run = RunManagement(repository, management).create_run(target(), dataset_id=LOAN_DATASET.id)
    monkeypatch.setattr(
        "agentgate.integrations.job_dispatchers.execution.load_judge_model_from_environment",
        lambda: configuration,
    )

    assert execute_evaluation_run.run(run.id) == RunStatus.COMPLETED.value

    assert len(repository.list_results(run.id)) == len(management.default_specs)
    assert len(client.requests) == len(LOAN_DATASET_VERSION.cases)
    assert client.closed is True


def test_worker_closes_judge_client_when_composition_fails(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "judge-composition-failure.db"
    monkeypatch.setenv("AGENTGATE_DB", str(database_path))
    repository = SQLiteRepository(database_path)
    seed_demo(repository)
    management = build_default_evaluator_management(repository)
    run = RunManagement(repository, management).create_run(target(), dataset_id=LOAN_DATASET.id)
    client = RecordingJudgeClient()
    configuration = configured_judge(client)
    monkeypatch.setattr(
        "agentgate.integrations.job_dispatchers.execution.load_judge_model_from_environment",
        lambda: configuration,
    )

    def fail_composition(*args, **kwargs) -> None:
        del args
        del kwargs
        raise ValueError("composition failed")

    monkeypatch.setattr(
        "agentgate.integrations.job_dispatchers.execution.build_default_evaluator_management",
        fail_composition,
    )

    with pytest.raises(ValueError, match="composition failed"):
        execute_evaluation_run.run(run.id)

    assert client.closed is True


def test_worker_rejects_unknown_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTGATE_DB", str(tmp_path / "unknown.db"))

    with pytest.raises(ValueError, match="unknown EvaluationRun"):
        execute_evaluation_run.run("missing")
