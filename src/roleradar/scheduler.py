"""Periodic scouting, run inside the app process.

The original shipped a macOS launchd plist with placeholder paths the user had
to hand-edit, plus a zsh script using BSD-only flags. That is three
platform-specific things to get wrong before the first run. An asyncio task in
the app's lifespan works the same everywhere and needs no setup at all.

For people who would rather the OS owned the schedule, `roleradar run --now`
is a one-line cron or Task Scheduler entry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from roleradar.prefs import load_prefs
from roleradar.scout import runner

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop() -> None:
    while True:
        prefs = load_prefs()
        interval = max(60, prefs.interval_minutes * 60)
        # Jitter keeps every install of this tool from hitting the same
        # volunteer-run feeds on the same minute.
        await asyncio.sleep(interval + random.uniform(0, 60))

        if not load_prefs().schedule_enabled:
            continue
        if runner.is_running():
            logger.info("scheduled run skipped: one is already in flight")
            continue
        try:
            await runner.run_scout()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed run must not kill the loop
            logger.exception("scheduled run failed")


def start() -> None:
    global _task
    if _task is not None:
        return
    if not load_prefs().schedule_enabled:
        logger.info("scheduler disabled by preferences")
        return
    _task = asyncio.create_task(_loop(), name="roleradar-scheduler")
    logger.info("scheduler started")


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None
