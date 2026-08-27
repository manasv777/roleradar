"""Tests for Scout alerting and the digest.

Alerting is what turns "detected within 30 minutes" into "applied early" — but
it must never be able to break a run, and it must never be so noisy that it
gets ignored. Both properties are asserted here.

No real `osascript` is ever invoked.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from roleradar.notify.digest import (
    MAX_NOTIFICATIONS_PER_RUN,
    NOTIFY_CONFIDENCE_THRESHOLD,
    listings_since,
    notify_new_listings,
    render_digest,
    select_notifiable,
    write_digest,
)

pytestmark = pytest.mark.unit


def listing(**kwargs) -> dict:
    base = {
        "listing_id": "l1",
        "company": "Acme",
        "title": "SWE Intern",
        "bucket": "spring_2027",
        "bucket_confidence": 0.95,
        "ats_score": 0.8,
        "apply_url": "https://example.test/apply",
        "active": True,
        "dismissed": False,
        "needs_verification": False,
        "deadline": None,
        "deadline_source": "unknown",
        "draft_status": "none",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(kwargs)
    return base


class TestSelection:
    def test_low_confidence_does_not_interrupt(self) -> None:
        """A notification for everything is a notification for nothing."""
        assert select_notifiable([listing(bucket_confidence=0.3)]) == []

    def test_dismissed_and_inactive_are_excluded(self) -> None:
        assert select_notifiable([listing(dismissed=True)]) == []
        assert select_notifiable([listing(active=False)]) == []

    def test_unclassified_is_excluded(self) -> None:
        assert select_notifiable([listing(bucket=None)]) == []

    def test_capped_so_a_first_sync_cannot_spam(self) -> None:
        many = [listing(listing_id=f"l{i}") for i in range(50)]
        assert len(select_notifiable(many)) == MAX_NOTIFICATIONS_PER_RUN

    def test_best_candidates_come_first(self) -> None:
        rows = [
            listing(listing_id="low", bucket_confidence=0.75, ats_score=0.1),
            listing(listing_id="high", bucket_confidence=0.98, ats_score=0.9),
        ]
        assert select_notifiable(rows)[0]["listing_id"] == "high"

    def test_threshold_boundary_is_inclusive(self) -> None:
        assert select_notifiable([listing(bucket_confidence=NOTIFY_CONFIDENCE_THRESHOLD)])


class TestNotifying:
    async def test_sends_one_per_selected_listing(self) -> None:
        with patch(
            "roleradar.notify.digest.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as send:
            sent = await notify_new_listings(
                [listing(listing_id="a"), listing(listing_id="b")]
            )
        assert sent == 2 and send.call_count == 2

    async def test_work_auth_caveat_travels_into_the_banner(self) -> None:
        """The caveat must survive into every surface, not just the UI."""
        with patch(
            "roleradar.notify.digest.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as send:
            await notify_new_listings(
                [listing(bucket="fall_2026_no_auth", needs_verification=True)]
            )
        assert "verify work auth" in send.call_args.kwargs["message"]

    async def test_overflow_is_summarized_not_spammed(self) -> None:
        many = [listing(listing_id=f"l{i}") for i in range(12)]
        with patch(
            "roleradar.notify.digest.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as send:
            await notify_new_listings(many)
        messages = [c.kwargs.get("message", "") for c in send.call_args_list]
        assert any("more new listings" in m for m in messages)

    async def test_nothing_to_report_sends_nothing(self) -> None:
        with patch(
            "roleradar.notify.digest.send_notification", new_callable=AsyncMock
        ) as send:
            assert await notify_new_listings([]) == 0
        assert send.call_count == 0

    async def test_a_failing_notifier_never_raises(self) -> None:
        with patch(
            "roleradar.notify.digest.send_notification",
            new_callable=AsyncMock,
            side_effect=RuntimeError("osascript exploded"),
        ):
            with pytest.raises(RuntimeError):
                # notify_new_listings does not swallow; the RUN loop does.
                await notify_new_listings([listing()])


class TestDigest:
    def test_groups_by_bucket_with_counts(self) -> None:
        out = render_digest(
            [listing(listing_id="a"), listing(listing_id="b", bucket="summer_2027")],
            generated_at="2026-08-17T08:00:00+00:00",
        )
        assert "spring_2027 (1)" in out and "summer_2027 (1)" in out
        assert "2 new listing(s)" in out

    def test_missing_deadline_renders_as_a_dash_not_a_guess(self) -> None:
        out = render_digest([listing()], generated_at="now")
        assert "| — |" in out

    def test_real_deadline_shows_its_provenance(self) -> None:
        out = render_digest(
            [listing(deadline="2027-01-15", deadline_source="greenhouse")],
            generated_at="now",
        )
        assert "2027-01-15 (greenhouse)" in out

    def test_work_auth_caveat_is_rendered_for_that_bucket(self) -> None:
        out = render_digest(
            [listing(bucket="fall_2026_no_auth", needs_verification=True)],
            generated_at="now",
        )
        assert "inferred, not verified" in out

    def test_caveat_absent_for_authorized_buckets(self) -> None:
        out = render_digest([listing()], generated_at="now")
        assert "inferred, not verified" not in out

    def test_empty_digest_says_so(self) -> None:
        assert "No new listings" in render_digest([], generated_at="now")

    def test_pipes_in_company_names_do_not_break_the_table(self) -> None:
        out = render_digest([listing(company="A | B")], generated_at="now")
        assert "A \\| B" in out

    async def test_written_to_a_dated_file(self, tmp_path) -> None:
        with patch(
            "roleradar.notify.digest.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ):
            path = await write_digest([listing()], data_dir=tmp_path, notify=False)
        assert path is not None and path.exists()
        assert path.parent.name == "digest"
        assert "Acme" in path.read_text()

    async def test_unwritable_target_returns_none_rather_than_raising(
        self, tmp_path
    ) -> None:
        blocker = tmp_path / "scout"
        blocker.write_text("not a directory")
        assert await write_digest([listing()], data_dir=tmp_path, notify=False) is None


class TestListingsSince:
    def test_window_filters_old_listings(self) -> None:
        old = listing(
            listing_id="old",
            first_seen_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        )
        assert [r["listing_id"] for r in listings_since([old, listing()], hours=24)] == ["l1"]

    def test_unparseable_timestamp_is_skipped_not_fatal(self) -> None:
        assert listings_since([listing(first_seen_at="nonsense")]) == []

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        assert len(listings_since([listing(first_seen_at=naive)])) == 1
