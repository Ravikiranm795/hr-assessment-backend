"""Worker entry point (``python -m app.jobs.scheduler``): APScheduler runs every job on its schedule.

Each run is logged with its result, one instance at a time, and an exception never kills the worker.
Run a single worker; the jobs are idempotent and row-locked, so an accidental second one is safe."""

import asyncio
import logging
import signal
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.jobs import JOBS

logger = logging.getLogger("app.jobs.scheduler")

SCHEDULES = {
    "expire_invites": IntervalTrigger(minutes=5),
    "timeout_attempts": IntervalTrigger(minutes=1),
    "retry_ai_scoring": IntervalTrigger(minutes=5),
    "retention_purge": CronTrigger(hour=2, minute=30),
    "version_purge": CronTrigger(hour=3, minute=0),
    "audit_partitions": CronTrigger(day=1, hour=1, minute=0),
    "notification_cleanup": CronTrigger(hour=3, minute=30),
    "report_cleanup": CronTrigger(hour=4, minute=0),
    "auth_cleanup": CronTrigger(hour=4, minute=30),
}


async def run_logged(name: str) -> dict | None:
    started = time.monotonic()
    logger.info("job %s start", name)
    try:
        result = await JOBS[name]()
    except Exception:  # noqa: BLE001 - keep the worker alive; the next tick retries
        logger.exception("job %s failed", name)
        return None
    logger.info("job %s done in %.2fs: %s", name, time.monotonic() - started, result)
    return result


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    for name, trigger in SCHEDULES.items():
        scheduler.add_job(
            run_logged, trigger, args=[name], id=name, max_instances=1, coalesce=True, misfire_grace_time=300
        )
    return scheduler


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    scheduler = build_scheduler()
    scheduler.start()
    await run_logged("audit_partitions")  # make sure next month's partitions exist at startup
    logger.info("worker started: %s", ", ".join(SCHEDULES))
    await stop.wait()
    scheduler.shutdown(wait=False)
    logger.info("worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
