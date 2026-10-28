"""Logging configuration with console, file, and error file appenders."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from time import monotonic

_PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
DEFAULT_LOG_PATH = str(_PROJECT_ROOT.parent / "runtime" / "logs" / "agentgate")

LOG_FORMAT = (
    "[%(asctime)s] [%(threadName)s] [%(levelname)s] %(name)s - %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3


class _ErrorOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class _PollingSuccessFilter(logging.Filter):
    _paths = frozenset({
        "/api/overview",
        "/api/runs",
        "/api/evaluation-tasks",
        "/api/evaluation-overview/summary",
        "/api/evaluation-task-summaries",
    })

    def __init__(self) -> None:
        super().__init__()
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or record.levelno != logging.INFO:
            return True
        # Uvicorn access records carry client, method, path, HTTP version, status.
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _, method, path, _, status = record.args
        if not (
            method == "GET"
            and isinstance(path, str)
            and path.partition("?")[0] in self._paths
            and isinstance(status, int)
            and 200 <= status < 300
        ):
            return True
        path = path.partition("?")[0]
        with self._lock:
            now = monotonic()
            previous = self._windows.get(path)
            if previous is None:
                self._windows[path] = (now, 0)
                return True
            last_logged, suppressed = previous
            if now - last_logged < 60:
                self._windows[path] = (last_logged, suppressed + 1)
                return False
            self._windows[path] = (now, 0)
        # Request-triggered, per-process summary; preserve Uvicorn's original arguments.
        record.msg = (
            f"{record.msg} [polling_success_count={suppressed + 1}"
            f" suppressed_success={suppressed} elapsed_seconds={now - last_logged:.1f}]"
        )
        return True


def setup_logging() -> None:
    log_path = Path(os.getenv("AGENTGATE_LOG_PATH", DEFAULT_LOG_PATH))
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if any(getattr(handler, "_agentgate_handler", False) for handler in root.handlers):
        return

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    console_handler._agentgate_handler = True
    root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_path / "agentgate.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler._agentgate_handler = True
    root.addHandler(file_handler)

    error_handler = RotatingFileHandler(
        log_path / "agentgate-error.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler._agentgate_handler = True
    root.addHandler(error_handler)

    logging.getLogger("agentgate").setLevel(logging.DEBUG)

    for name in ("uvicorn", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    logging.getLogger("uvicorn.access").addFilter(_PollingSuccessFilter())

    logging.getLogger("agentgate.server.logging_config").info(
        "Logging initialized, log_path=%s", log_path
    )


_SENSITIVE_ENV_VARS = frozenset({
    "AGENTGATE_TDSQL_PASSWORD",
    "AGENTGATE_JUDGE_API_KEY",
    "AGENTGATE_JUDGE_SIGNATURE_HEX",
    "AGENTGATE_JUDGE_ORIGINAL_DIGEST_HEX",
    "AGENTGATE_JUDGE_SESSION_ID",
    "AGENTGATE_API_KEY_ENCRYPTION_KEY",
})

_USED_ENV_VARS = (
    ("AGENTGATE_DB_TYPE", "sqlite"),
    ("AGENTGATE_DB", "agentgate.db"),
    ("AGENTGATE_TDSQL_URL", ""),
    ("AGENTGATE_TDSQL_USER", ""),
    ("AGENTGATE_TDSQL_PASSWORD", ""),
    ("AGENT_TASK_DISPATCHER_TYPE", "celery"),
    ("AGENTGATE_SCHEDULER_INTERVAL_SECONDS", "10"),
    ("AGENTGATE_MAX_CONCURRENT_RUNS_PER_API_KEY", "10"),
    ("AGENTGATE_PENDING_TIMEOUT_SECONDS", "1800"),
    ("AGENTGATE_MAX_DISPATCH_ATTEMPTS", "10"),
    ("AGENTGATE_REDIS_URL", "redis://localhost:6379/0"),
    ("AGENTGATE_REDIS_MODE", "single"),
    ("AGENTGATE_REDIS_CLUSTER_HASH_TAG", "{agentgate}"),
    ("AGENTGATE_WORKER_CONCURRENCY", "1"),
    ("AGENTGATE_TASK_TIME_LIMIT_SECONDS", "360"),
    ("AGENTGATE_BJS_SUBMIT_URL", ""),
    ("AGENTGATE_BJS_JOB_ID", ""),
    ("AGENTGATE_JUDGE_PROVIDER_ID", ""),
    ("AGENTGATE_JUDGE_BASE_URL", ""),
    ("AGENTGATE_JUDGE_API_KEY", ""),
    ("AGENTGATE_JUDGE_MODEL_ID", ""),
    ("AGENTGATE_JUDGE_TRANSPORT", "api"),
    ("AGENTGATE_JUDGE_SIGNATURE_HEX", ""),
    ("AGENTGATE_JUDGE_ORIGINAL_DIGEST_HEX", ""),
    ("AGENTGATE_JUDGE_SESSION_ID", ""),
    ("AGENTGATE_API_KEY_ENCRYPTION_KEY", ""),
    ("AGENTGATE_LOG_PATH", ""),
    ("AGENTGATE_BANK_BASE_URL", "http://127.0.0.1:8107"),
    ("AGENTGATE_AGENT_PLATFORM_MODE", ""),
    ("AGENTGATE_AGENT_PLATFORM_ORIGIN", "http://127.0.0.1:8119"),
)


def _mask_sensitive(value: str) -> str:
    if not value:
        return "(empty)"
    return value[:2] + "***"


def log_environment_summary() -> None:
    logger = logging.getLogger("agentgate.environment")
    logger.info("=== Environment Variables ===")
    for name, default in _USED_ENV_VARS:
        value = os.getenv(name, default)
        if name in _SENSITIVE_ENV_VARS:
            value = _mask_sensitive(value)
        logger.info("  %s=%s", name, value)
    logger.info("=== End Environment Variables ===")
