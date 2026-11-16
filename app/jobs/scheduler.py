"""Worker entry point (``python -m app.jobs.scheduler``).

Placeholder until the job layer lands: idles and exits cleanly on SIGTERM/SIGINT so the
worker container is healthy and stoppable.
"""

import asyncio
import logging
import signal

logger = logging.getLogger("app.jobs.scheduler")


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    logger.info("worker started; no jobs registered yet")
    await stop.wait()
    logger.info("worker stopping")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
