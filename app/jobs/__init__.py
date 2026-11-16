"""Background jobs (spec 9). ``JOBS`` maps a job name to its ``run`` coroutine; the scheduler and
``python -m app.cli run-job <name>`` both dispatch through it."""

from collections.abc import Awaitable, Callable

from app.jobs import (
    audit_partitions,
    auth_cleanup,
    expire_invites,
    notification_cleanup,
    report_cleanup,
    retention_purge,
    retry_ai_scoring,
    timeout_attempts,
    version_purge,
)

JobRun = Callable[..., Awaitable[dict]]

JOBS: dict[str, JobRun] = {
    module.NAME: module.run
    for module in (
        expire_invites, timeout_attempts, retry_ai_scoring, retention_purge, version_purge,
        audit_partitions, notification_cleanup, report_cleanup, auth_cleanup,
    )
}  # fmt: skip

__all__ = ["JOBS", "JobRun"]
