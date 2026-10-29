"""Celery/Redis delivery for persisted Evaluation Runs."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import closing
from urllib.parse import urlsplit

from celery import Celery
from celery.app.task import Task

from agentgate.application import RunScheduling
from agentgate.integrations.job_dispatchers.configuration import create_dispatcher
from agentgate.integrations.job_dispatchers.execution import execute_persisted_run
from agentgate.storage.configuration import create_repository, load_database_config

LOGGER = logging.getLogger(__name__)

TASK_NAME = "agentgate.execute_evaluation_run"
SCHEDULER_TASK_NAME = "agentgate.dispatch_due_evaluation_runs"
SCHEDULER_QUEUE = "agentgate.scheduler"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_MODE = "single"
DEFAULT_REDIS_CLUSTER_HASH_TAG = "{agentgate}"
REDIS_CLUSTER_TRANSPORT = "agentgate.integrations.job_dispatchers.redis_cluster_transport:Transport"
DEFAULT_TASK_TIME_LIMIT_SECONDS = 360
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 10


def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _redis_broker_configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, object]]:
    settings = os.environ if environ is None else environ
    broker_url = settings.get("AGENTGATE_REDIS_URL", DEFAULT_REDIS_URL).strip()
    mode = settings.get("AGENTGATE_REDIS_MODE", DEFAULT_REDIS_MODE).strip().lower()

    if mode == "single":
        return broker_url, {}
    if mode != "cluster":
        raise ValueError("AGENTGATE_REDIS_MODE must be single or cluster")

    parsed_url = urlsplit(broker_url)
    if parsed_url.scheme not in {"redis", "rediss"}:
        raise ValueError("AGENTGATE_REDIS_URL must use redis:// or rediss:// in cluster mode")
    if parsed_url.path not in {"", "/", "/0"}:
        raise ValueError("Redis Cluster supports only database 0")

    hash_tag = settings.get(
        "AGENTGATE_REDIS_CLUSTER_HASH_TAG",
        DEFAULT_REDIS_CLUSTER_HASH_TAG,
    ).strip()
    if (
        len(hash_tag) < 3
        or not hash_tag.startswith("{")
        or not hash_tag.endswith("}")
        or "{" in hash_tag[1:-1]
        or "}" in hash_tag[1:-1]
    ):
        raise ValueError("AGENTGATE_REDIS_CLUSTER_HASH_TAG must be a non-empty Redis hash tag")

    return broker_url, {
        "broker_transport": REDIS_CLUSTER_TRANSPORT,
        "broker_transport_options": {"hash_tag": hash_tag},
    }


def create_celery_app() -> Celery:
    """Build the process-local Celery application from environment settings."""

    broker_url, broker_configuration = _redis_broker_configuration()
    app = Celery(
        "agentgate",
        broker=broker_url,
    )
    scheduler_interval = _positive_int_setting(
        "AGENTGATE_SCHEDULER_INTERVAL_SECONDS",
        DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    )
    app.conf.update(
        **broker_configuration,
        accept_content=["json"],
        result_backend=None,
        result_serializer="json",
        task_acks_late=True,
        task_ignore_result=True,
        task_reject_on_worker_lost=False,
        task_serializer="json",
        task_store_errors_even_if_ignored=False,
        task_time_limit=_positive_int_setting(
            "AGENTGATE_TASK_TIME_LIMIT_SECONDS",
            DEFAULT_TASK_TIME_LIMIT_SECONDS,
        ),
        worker_concurrency=_positive_int_setting("AGENTGATE_WORKER_CONCURRENCY", 1),
        worker_prefetch_multiplier=1,
        task_routes={
            SCHEDULER_TASK_NAME: {"queue": SCHEDULER_QUEUE},
        },
        beat_schedule={
            "dispatch-due-evaluation-runs": {
                "task": SCHEDULER_TASK_NAME,
                "schedule": scheduler_interval,
                "options": {"queue": SCHEDULER_QUEUE},
            }
        },
    )
    return app


celery_app = create_celery_app()


@celery_app.task(
    name=TASK_NAME,
    acks_late=True,
    ignore_result=True,
    reject_on_worker_lost=False,
)
def execute_evaluation_run(run_id: str) -> str:
    """Load one persisted Run and execute it through the shared application boundary."""

    try:
        return execute_persisted_run(run_id)
    except Exception:
        LOGGER.error("Worker execution failed: run_id=%s", run_id, exc_info=True)
        raise


@celery_app.task(
    name=SCHEDULER_TASK_NAME,
    ignore_result=True,
    queue=SCHEDULER_QUEUE,
)
def dispatch_due_evaluation_runs() -> int:
    """Release due scheduled Runs and submit them to the execution queue."""

    dispatcher = create_dispatcher()
    with closing(create_repository(load_database_config())) as repository:
        scheduling = RunScheduling(repository)
        scheduling.recover_stale_runs(dispatcher)
        due = scheduling.dispatch_due_runs(dispatcher)
        waiting = scheduling.dispatch_waiting_runs(dispatcher)
        return len(due) + len(waiting)


class CeleryJobDispatcher:
    """Submit persisted Run IDs to the configured Celery broker."""

    def __init__(self, task: Task | None = None) -> None:
        self.task = task or execute_evaluation_run

    def submit(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be blank")
        self.task.apply_async(args=[run_id], task_id=run_id)

    def cancel(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be blank")
        self.task.app.control.revoke(run_id, terminate=False)
