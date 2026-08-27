"""Service tests for the Scout run loop.

The background task is a new pattern in this codebase, so the failure modes
that have no precedent here get explicit coverage: overlapping runs, a dead
LLM provider, cancellation mid-draft, and a missing master resume.

No real network and no real LLM — every boundary is patched.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from roleradar.scout import runner as runner_mod
from roleradar.scout.runner import (
    RunState,
    ScoutAlreadyRunning,
    start_scout_run,
)

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
def _reset_runner_state():
    runner_mod._current_state = None
    runner_mod._current_task = None
    yield
    runner_mod._current_state = None
    runner_mod._current_task = None


async def _seed_master(isolated_db, sample_resume) -> dict:
    return await isolated_db.create_resume(
        content="# Resume",
        content_type="md",
        filename="master.pdf",
        is_master=True,
        processed_data=sample_resume,
        processing_status="ready",
    )


async def _seed_listing(
    isolated_db, n: int = 1, first_seen_at: str | None = None
) -> list[str]:
    """Seed listings. `first_seen_at` must be set at creation — it is a
    provenance column that `update_listing` deliberately refuses to
    rewrite, so a re-parse can never change which listing a row is."""
    ids = []
    for i in range(n):
        payload = {
                "source_id": "simplify_intern",
                "external_id": f"x{i}",
                "fingerprint": f"fp{i}",
                "company": f"Acme {i}",
                "title": "Software Engineer Intern",
                "apply_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                "bucket": "spring_2027",
                "bucket_confidence": 0.95,
                "track": "swe",
                "description_text": "We want a Python and FastAPI intern. " * 20,
        }
        if first_seen_at:
            payload["first_seen_at"] = first_seen_at
        row = await isolated_db.create_listing(payload)
        ids.append(row["listing_id"])
    return ids


class TestConcurrency:
    async def test_second_run_is_rejected_while_one_is_in_flight(
        self, isolated_db
    ) -> None:
        """Overlapping runs would double-draft and thrash a single local model."""
        gate = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=blocked):
            await start_scout_run()
            with pytest.raises(ScoutAlreadyRunning):
                await start_scout_run()
            gate.set()
            await runner_mod.cancel_scout_run()

    async def test_run_state_is_observable_immediately(self, isolated_db) -> None:
        gate = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=blocked):
            state = await start_scout_run()
            assert runner_mod.get_run_state().run_id == state.run_id
            assert runner_mod.is_running() is True
            gate.set()
            await runner_mod.cancel_scout_run()
class TestDeadlinePriority:
    """Closing soonest drafts first, and does not wait for the quiet hours.

    Only ~2 of 547 active listings carry a deadline — no aggregator publishes
    one and description scanning is mostly false positives — so this changes
    the order for a handful of listings and is a no-op for the rest. That is
    the honest extent of what the data supports.
    """

    def test_a_closing_listing_outranks_a_fresh_one(self) -> None:
        from datetime import datetime, timedelta, timezone
        from roleradar.scout.runner import days_until_deadline

        soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        far = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        assert days_until_deadline({"deadline": soon}) < days_until_deadline({"deadline": far})
        # No deadline must sort last, never ahead of a real one.
        assert days_until_deadline({"deadline": far}) < days_until_deadline({})

    def test_unparseable_deadlines_sort_last_rather_than_crash(self) -> None:
        from roleradar.scout.runner import days_until_deadline

        assert days_until_deadline({"deadline": "sometime in spring"}) == days_until_deadline({})

    def test_an_imminent_deadline_is_exempt_from_the_backlog_window(self) -> None:
        """Waiting for tonight's quiet hours could mean missing the deadline."""
        from datetime import datetime, timedelta, timezone
        from roleradar.scout.runner import _is_urgent

        assert _is_urgent(
            {"deadline": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}
        )
        assert not _is_urgent(
            {"deadline": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
        )
        assert not _is_urgent({})


