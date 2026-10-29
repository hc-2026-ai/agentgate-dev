"""Release due Runs without Redis; run continuously or as one external scheduler tick."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from contextlib import closing
from threading import Event

from agentgate.application import RunScheduling
from agentgate.integrations.job_dispatchers.configuration import create_dispatcher
from agentgate.storage.configuration import create_repository, load_database_config


LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Release due Runs once and exit.")
    parser.add_argument(
        "--name",
        default="",
        help="informational instance label used to tell stacks apart in process lists",
    )
    options = parser.parse_args()
    interval = int(os.getenv("AGENTGATE_SCHEDULER_INTERVAL_SECONDS", "10"))
    if interval < 1:
        raise ValueError("AGENTGATE_SCHEDULER_INTERVAL_SECONDS must be at least 1")
    dispatcher = create_dispatcher()
    config = load_database_config()
    stopped = Event()

    def _handle_stop(signum, *_):
        LOGGER.info("BJS scheduler received stop signal (%s), shutting down", signum)
        stopped.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    LOGGER.info(
        "BJS scheduler started (interval=%ds%s)", interval, f", name={options.name}" if options.name else ""
    )
    while not stopped.is_set():
        with closing(create_repository(config)) as repository:
            scheduling = RunScheduling(repository)
            scheduling.recover_stale_runs(dispatcher)
            due = scheduling.dispatch_due_runs(dispatcher)
            waiting = scheduling.dispatch_waiting_runs(dispatcher)
        total = len(due) + len(waiting)
        print(f"Dispatched {total} Runs ({len(due)} due, {len(waiting)} waiting)", flush=True)
        if options.once:
            return 0
        stopped.wait(interval)
    LOGGER.info("BJS scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
