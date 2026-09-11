"""Worker entrypoint.

Runs the scheduler in its own process so periodic jobs never compete with API
request handling, and so the API can be scaled horizontally without running
the schedule N times.
"""
from __future__ import annotations

import signal
import sys
import threading

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.database.session import check_database
from app.workers.scheduler import scheduler_status, start_scheduler, stop_scheduler

log = get_logger(__name__)
_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("worker_signal_received", signal=signum)
    _stop.set()


def main() -> int:
    configure_logging()

    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            log.error("configuration_error", problem=problem)
        if settings.is_production:
            log.error("worker_refusing_to_start")
            return 1

    if not check_database():
        log.error("worker_database_unreachable")
        return 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    scheduler = start_scheduler()
    if scheduler is None:
        log.error("worker_scheduler_disabled", hint="set SCHEDULER_ENABLED=true")
        return 1

    # The tick stream lives in the worker only: a second process would open a
    # second Kite connection against the same access token.
    tick_service = None
    if settings.kite_streaming_enabled:
        from app.market_data.tick_service import get_tick_service

        tick_service = get_tick_service()
        if tick_service.start():
            log.info("worker_tick_stream_started", status=tick_service.status())
        else:
            log.warning(
                "worker_tick_stream_unavailable",
                hint="check KITE_API_KEY / KITE_ACCESS_TOKEN and the subscription",
            )

    log.info("worker_started", jobs=scheduler_status()["jobs"])
    try:
        _stop.wait()
    finally:
        if tick_service is not None:
            tick_service.stop()
        stop_scheduler()
        log.info("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
