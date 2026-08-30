"""Unit tests for Scout persistence, against a real (isolated) SQLite database.

The assertions that matter are the state-preservation ones: a feed republishing
a row must never destroy a drafted resume, a tracker card, a hand-typed
deadline, or a dismissal — and a listing that vanishes upstream must be retired
rather than deleted, because drafts still reference it.
"""

import pytest

from datetime import date

from roleradar.prefs import Preferences
from roleradar.scout.normalize import ScoutRecord
from roleradar.scout.store import upsert_listing as _upsert_listing
from roleradar.scout.store import upsert_records as _upsert_records
from roleradar.taxonomy import load_taxonomy
from roleradar.terms import horizon_terms

pytestmark = pytest.mark.unit

TAXONOMY = load_taxonomy()
HORIZON = horizon_terms(date(2026, 8, 29))


async def upsert_listing(record, *, prefs: Preferences | None = None):
    """Classification context is per-run, not per-record; tests supply defaults."""
    return await _upsert_listing(
        record, prefs=prefs or Preferences(), taxonomy=TAXONOMY, horizon=HORIZON
    )


async def upsert_records(records, source_id, *, prefs: Preferences | None = None):
    return await _upsert_records(
        records, source_id, prefs=prefs or Preferences(), taxonomy=TAXONOMY, horizon=HORIZON
    )


def rec(**kwargs: object) -> ScoutRecord:
    base: dict = {
        "source_id": "simplify_intern",
        "external_id": "x1",
        "company": "Acme",
        "title": "Software Engineer Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "category": "Software",
        "terms": ["Spring 2027"],
        "date_updated": "2026-08-01T00:00:00+00:00",
    }
    base.update(kwargs)
    return ScoutRecord(**base)  # type: ignore[arg-type]


class TestUpsertCreate:
    async def test_in_scope_record_is_created(self, isolated_db) -> None:
        listing_id, outcome = await upsert_listing(rec())
        assert outcome == "created" and listing_id

        row = await isolated_db.get_listing(listing_id)
        assert row["term_id"] == "spring_2027"
        assert row["role_type"] == "internship"
        assert row["ats_vendor"] == "greenhouse" and row["ats_tenant"] == "acme"
        assert row["classify_reasons"], "the classification must be auditable"

    async def test_a_listing_outside_the_horizon_is_still_stored(self, isolated_db) -> None:
        """INVERSION. The original asserted this row was discarded, on the
        grounds that only the filtered shortlist should ever be stored.

        That is what made widening your interests require refetching the world:
        a listing dropped at ingest cannot be recovered by reclassifying. It is
        stored now, unlabelled, and filtered at query time instead.
        """
        listing_id, outcome = await upsert_listing(rec(terms=["Summer 2019"]))
        assert outcome == "created"
        assert listing_id is not None

        row = await isolated_db.get_listing(listing_id)
        assert row["term_id"] is None                 # genuinely outside the horizon
        assert row["role_type"] == "internship"       # but still a real internship
        assert row["active"] is True                  # and still open upstream

    async def test_a_non_engineering_listing_is_not_stored(self, isolated_db) -> None:
        """Store-everything is not store-anything: a sales role is still noise."""
        listing_id, outcome = await upsert_listing(rec(title="Sales Development Intern"))
        assert (listing_id, outcome) == (None, "skipped")
        assert await isolated_db.count_listings() == 0

    async def test_work_auth_is_scored_only_when_the_user_needs_it(
        self, isolated_db
    ) -> None:
        """INVERSION. The original folded work authorization into the term
        itself, producing a `fall_2026_no_auth` bucket for one person's visa
        situation. Term and authorization are separate facts now, and the
        second is only computed when the user says they need sponsorship."""
        from roleradar.prefs import Preferences, WorkAuth

        record = rec(terms=["Fall 2026"], locations=["Toronto, ON, Canada"])

        listing_id, _ = await upsert_listing(record)
        row = await isolated_db.get_listing(listing_id)
        assert row["term_id"] == "fall_2026"          # the term, and only the term
        assert row["work_auth_score"] is None
        assert row["needs_verification"] is False

        sponsored = Preferences(work_auth=WorkAuth(requires_sponsorship=True))
        listing_id, _ = await upsert_listing(
            rec(external_id="x2", terms=["Fall 2026"],
                locations=["Toronto, ON, Canada"]),
            prefs=sponsored,
        )
        row = await isolated_db.get_listing(listing_id)
        assert row["work_auth_score"] is not None
        assert row["needs_verification"] is True


class TestChangeDetection:
    async def test_same_revision_is_unchanged(self, isolated_db) -> None:
        listing_id, _ = await upsert_listing(rec())
        before = await isolated_db.get_listing(listing_id)

        again_id, outcome = await upsert_listing(rec())
        assert (again_id, outcome) == (listing_id, "unchanged")

        after = await isolated_db.get_listing(listing_id)
        assert after["last_seen_at"] >= before["last_seen_at"]

    async def test_newer_revision_reclassifies(self, isolated_db) -> None:
        listing_id, _ = await upsert_listing(rec())
        _, outcome = await upsert_listing(
            rec(terms=["Summer 2027"], date_updated="2026-08-09T00:00:00+00:00")
        )
        assert outcome == "updated"
        assert (await isolated_db.get_listing(listing_id))["term_id"] == "summer_2027"

    async def test_closing_upstream_is_an_update(self, isolated_db) -> None:
        listing_id, _ = await upsert_listing(rec())
        _, outcome = await upsert_listing(rec(active=False))
        assert outcome == "updated"
        assert (await isolated_db.get_listing(listing_id))["active"] is False


class TestStatePreservation:
    async def test_manual_deadline_is_never_overwritten(self, isolated_db) -> None:
        listing_id, _ = await upsert_listing(rec())
        await isolated_db.update_listing(
            listing_id, {"deadline": "2027-01-15", "deadline_source": "manual"}
        )

        await upsert_listing(rec(date_updated="2026-08-09T00:00:00+00:00"))

        row = await isolated_db.get_listing(listing_id)
        assert row["deadline"] == "2027-01-15"
        assert row["deadline_source"] == "manual"

    async def test_dismissal_survives_a_reparse(self, isolated_db) -> None:
        listing_id, _ = await upsert_listing(rec())
        await isolated_db.update_listing(listing_id, {"dismissed": True})

        await upsert_listing(rec(date_updated="2026-08-09T00:00:00+00:00"))

        assert (await isolated_db.get_listing(listing_id))["dismissed"] is True

    async def test_no_longer_matching_a_filter_does_not_retire_a_listing(
        self, isolated_db
    ) -> None:
        """INVERSION. The original set active=False when a listing stopped
        matching the user's fields, calling it "retired".

        That conflated two unrelated facts. `active` means "still listed
        upstream", and mark_listings_inactive is its only owner. A listing that
        no longer matches your interests is a query concern - it is still a real,
        open job, and flipping `active` would make it invisible to everyone,
        including a user who later widens their fields.
        """
        listing_id, _ = await upsert_listing(rec())

        # Re-parse the same listing with a term far outside the horizon.
        await upsert_listing(
            rec(terms=["Summer 2019"], date_updated="2026-08-09T00:00:00+00:00")
        )

        row = await isolated_db.get_listing(listing_id)
        assert row["active"] is True, "still open upstream, so still active"
        assert row["term_id"] is None, "but no longer placed in the horizon"

class TestCrossSourceDedupe:
    async def test_same_role_from_another_feed_corroborates(self, isolated_db) -> None:
        first_id, _ = await upsert_listing(rec())
        second_id, outcome = await upsert_listing(
            rec(source_id="zshah_intern", external_id="z9")
        )

        assert second_id == first_id, "a duplicate must not create a second row"
        assert outcome == "corroborated"
        assert await isolated_db.count_listings() == 1

        row = await isolated_db.get_listing(first_id)
        assert any("corroborated by zshah_intern" in r for r in row["classify_reasons"])

    async def test_corroboration_survives_tracking_param_differences(
        self, isolated_db
    ) -> None:
        """Feeds append their own tracking params to the same apply URL."""
        first_id, _ = await upsert_listing(
            rec(apply_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=simplify")
        )
        second_id, _ = await upsert_listing(
            rec(
                source_id="zshah_intern",
                external_id="z9",
                apply_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=zshah",
            )
        )
        assert second_id == first_id

    async def test_a_classifying_source_fills_in_for_one_that_cannot(
        self, isolated_db
    ) -> None:
        """vanshb03 has no year, so a year-carrying feed should complete it."""
        # Seed a row that a non-classifying source could not bucket.
        seeded = await isolated_db.create_listing(
            {
                "source_id": "vansh_intern",
                "external_id": "v1",
                "fingerprint": __import__(
                    "roleradar.scout.normalize", fromlist=["fingerprint"]
                ).fingerprint("Acme", "Software Engineer Intern",
                              "https://boards.greenhouse.io/acme/jobs/1"),
                "company": "Acme",
                "title": "Software Engineer Intern",
                "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                "term_id": None,
                "classify_confidence": 0.0,
                "classify_reasons": ["no term signal"],
            }
        )
        _, outcome = await upsert_listing(rec())
        assert outcome == "corroborated"

        row = await isolated_db.get_listing(seeded["listing_id"])
        assert row["term_id"] == "spring_2027"

    async def test_different_roles_do_not_collapse(self, isolated_db) -> None:
        await upsert_listing(rec())
        await upsert_listing(
            rec(
                source_id="zshah_intern",
                external_id="z9",
                title="ML Intern",
                apply_url="https://boards.greenhouse.io/acme/jobs/2",
                category="AI/ML/Data",
            )
        )
        assert await isolated_db.count_listings() == 2


class TestUpsertRecords:
    async def test_counts_each_outcome(self, isolated_db) -> None:
        stats = await upsert_records(
            [
                rec(external_id="a", apply_url="https://boards.greenhouse.io/acme/jobs/a"),
                rec(external_id="b", apply_url="https://boards.greenhouse.io/acme/jobs/b"),
                rec(external_id="c", terms=["Summer 2025"],
                    apply_url="https://boards.greenhouse.io/acme/jobs/c"),
            ],
            "simplify_intern",
        )
        # INVERSION: the third row is outside the term horizon. The original
        # counted it as skipped_out_of_scope; it is now stored unlabelled.
        assert stats.created == 3
        assert stats.skipped_out_of_scope == 0
        assert stats.errors == 0

    async def test_vanished_listing_is_retired_not_deleted(self, isolated_db) -> None:
        await upsert_records(
            [
                rec(external_id="a", apply_url="https://boards.greenhouse.io/acme/jobs/a"),
                rec(external_id="b", apply_url="https://boards.greenhouse.io/acme/jobs/b"),
            ],
            "simplify_intern",
        )
        # Second parse: "b" is gone from the feed.
        stats = await upsert_records(
            [rec(external_id="a", apply_url="https://boards.greenhouse.io/acme/jobs/a")],
            "simplify_intern",
        )
        assert stats.deactivated == 1
        assert await isolated_db.count_listings() == 1
        assert len(await isolated_db.list_listings(include_inactive=True)) == 2

    async def test_one_bad_record_does_not_abort_the_batch(self, isolated_db) -> None:
        good = rec(external_id="a", apply_url="https://boards.greenhouse.io/acme/jobs/a")
        # company=None violates a NOT NULL column at insert time.
        bad = rec(external_id="b", company=None,
                  apply_url="https://boards.greenhouse.io/acme/jobs/b")
        stats = await upsert_records([bad, good], "simplify_intern")
        assert stats.created == 1, "the good record must still land"


class TestAlreadyClosedListings:
    async def test_listing_closed_before_we_saw_it_is_skipped(
        self, isolated_db
    ) -> None:
        """Day-one noise: hundreds of dead rows with nothing to apply to."""
        listing_id, outcome = await upsert_listing(rec(active=False))
        assert (listing_id, outcome) == (None, "skipped")
        assert await isolated_db.count_listings() == 0

    async def test_listing_we_saw_open_is_kept_when_it_closes(
        self, isolated_db
    ) -> None:
        """The closure of a role we were tracking IS worth recording."""
        listing_id, _ = await upsert_listing(rec())
        _, outcome = await upsert_listing(rec(active=False))
        assert outcome == "updated"

        row = await isolated_db.get_listing(listing_id)
        assert row is not None and row["active"] is False
