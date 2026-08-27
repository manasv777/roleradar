"""Playwright browser singleton for job-description rendering.

Chromium is an optional dependency (`pip install 'roleradar[render]'`). Tiers 1
and 2 of the enrichment cascade cover the ATS boards that aggregator feeds link
to; rendering matters for direct company career sites. When Chromium is absent
this module returns None and the cascade degrades to those tiers rather than
failing, so the app runs fine on a machine that never installed it.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_browser = None
_lock = asyncio.Lock()
_unavailable_logged = False


async def get_shared_browser():
    """Return a shared Chromium instance, or None if it is not available.

    Never raises. Callers treat None as "rendering is off".
    """
    global _browser, _unavailable_logged

    if _browser is not None:
        return _browser

    async with _lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if not _unavailable_logged:
                logger.info(
                    "playwright not installed - job-description rendering disabled. "
                    "Install with: pip install 'roleradar[render]'"
                )
                _unavailable_logged = True
            return None

        try:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            # Most commonly "Executable doesn't exist" - playwright is installed
            # but `playwright install chromium` was never run.
            if not _unavailable_logged:
                logger.info("chromium unavailable - rendering disabled (%s)", exc)
                _unavailable_logged = True
            return None

    return _browser


async def close_shared_browser() -> None:
    """Shut the browser down. Safe to call when one was never opened."""
    global _browser
    if _browser is None:
        return
    try:
        await _browser.close()
    except Exception:
        logger.debug("browser close failed", exc_info=True)
    finally:
        _browser = None
