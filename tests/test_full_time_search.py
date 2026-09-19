"""Full-time role search: normalizers, query derivation, pagination, gating."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from roleradar.prefs import Preferences
from roleradar.scout.normalize import normalize_himalayas, normalize_jobicy
from roleradar.scout.sources import fetch_source
from roleradar.scout.sources.search import search_queries, search_specs
from roleradar.taxonomy import load_taxonomy

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "scout"
TAXONOMY = load_taxonomy()


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class TestNormalizers:
    def test_himalayas_records_are_full_time_and_labelled(self) -> None:
        records = [normalize_himalayas(j, "h") for j in fixture("himalayas_search")["jobs"]]
        assert records and all(r is not None for r in records)
        assert {r.role_type for r in records} == {"full_time"}
        # Hyphenated feed categories are normalised so the taxonomy can match them.
        assert all(r.category is None or "-" not in r.category for r in records)
        for r in records:
            fields = TAXONOMY.classify(r.title, r.category)
            assert "data" in fields.domains, r.title

    def test_jobicy_records_keep_the_feed_url(self) -> None:
        """Jobicy's terms require apply links to be the URL the feed provides."""
        for job in fixture("jobicy_search")["jobs"]:
            record = normalize_jobicy(job, "j")
            assert record is not None
            assert record.apply_url == job["url"]
            assert record.role_type == "full_time"

    def test_an_intern_title_beats_a_full_time_label(self) -> None:
        """Job boards mark plenty of internships "Full Time"."""
        job = dict(fixture("jobicy_search")["jobs"][0], jobTitle="Data Engineering Intern")
        assert normalize_jobicy(job, "j").role_type == "internship"

    def test_expired_himalayas_postings_are_inactive(self) -> None:
        job = dict(fixture("himalayas_search")["jobs"][0], expiryDate="1000000000")
        assert normalize_himalayas(job, "h").active is False


class TestQueries:
    def test_off_unless_full_time_is_selected(self) -> None:
        prefs = Preferences(full_time_searches=["data engineer"])
        assert search_specs(prefs, TAXONOMY) == []

    def test_explicit_searches_are_used_verbatim(self) -> None:
        prefs = Preferences(
            role_types=["full_time"],
            full_time_searches=["data engineer", "hadoop developer"],
        )
        assert search_queries(prefs, TAXONOMY) == ["data engineer", "hadoop developer"]
        ids = {s.source_id for s in search_specs(prefs, TAXONOMY)}
        assert ids == {
            "himalayas:data-engineer", "jobicy:data-engineer",
            "himalayas:hadoop-developer", "jobicy:hadoop-developer",
        }

    def test_searches_are_derived_from_fields_when_none_are_given(self) -> None:
        """No hardcoded default: picking Data Engineering is enough."""
        prefs = Preferences(role_types=["full_time"], interests={"data": ["data_engineering"]})
        assert search_queries(prefs, TAXONOMY) == ["Data Engineering"]

    def test_preferences_round_trip(self) -> None:
        prefs = Preferences(role_types=["full_time"], full_time_searches=["hadoop developer"])
        again = Preferences.from_dict(prefs.to_dict())
        assert again.full_time_searches == ["hadoop developer"]
        assert "full_time" in again.role_types


class TestPagination:
    @respx.mock
    async def test_pages_are_walked_and_deduplicated(self, monkeypatch) -> None:
        import roleradar.scout.sources.aggregators as agg

        monkeypatch.setattr(agg, "INTER_REQUEST_DELAY_SECONDS", 0)
        jobs = fixture("himalayas_search")["jobs"]
        spec = search_specs(
            Preferences(role_types=["full_time"], full_time_searches=["hadoop"]), TAXONOMY
        )[0]

        # Himalayas pages by 1-based `page` and ignores `offset` entirely. An
        # earlier version of this test mocked `offset`, so it passed against an
        # API that does not exist - the live run is what caught it.
        def page(request: httpx.Request) -> httpx.Response:
            assert "offset" not in request.url.params
            number = int(request.url.params.get("page", 1))
            chunk = {1: jobs[:2], 2: jobs[1:4], 3: []}.get(number, [])
            return httpx.Response(200, json={"jobs": chunk})

        respx.get(url__startswith="https://himalayas.app/").mock(side_effect=page)
        async with httpx.AsyncClient() as client:
            result = await fetch_source(spec, client=client)

        assert result.status == "ok"
        ids = [r.external_id for r in result.records]
        assert len(ids) == len(set(ids)) == 4   # overlap on page two is not double-counted

    @respx.mock
    async def test_a_dead_first_page_is_a_failed_source(self, monkeypatch) -> None:
        import roleradar.scout.sources.aggregators as agg

        monkeypatch.setattr(agg, "_RETRY_BACKOFF_SECONDS", ())
        spec = search_specs(
            Preferences(role_types=["full_time"], full_time_searches=["hadoop"]), TAXONOMY
        )[0]
        respx.get(url__startswith="https://himalayas.app/").mock(
            return_value=httpx.Response(404)
        )
        async with httpx.AsyncClient() as client:
            result = await fetch_source(spec, client=client)
        assert result.status == "error"


class TestReclassifyKeepsRoleType:
    """Regression: reclassify rebuilt records from columns and dropped
    role_type, so every plain "Data Engineer" became "unknown" the first time
    someone followed the README and ran it after editing the taxonomy."""

    async def test_full_time_survives_reclassify(self, isolated_db) -> None:
        from roleradar.scout.reclassify import reclassify_all
        from roleradar.scout.store import upsert_listing

        job = next(
            j for j in fixture("jobicy_search")["jobs"] if j["jobTitle"] == "Data Engineer"
        )
        record = normalize_jobicy(job, "jobicy:data-engineer")
        prefs = Preferences(role_types=["full_time"])
        listing_id, _ = await upsert_listing(
            record, prefs=prefs, taxonomy=TAXONOMY, horizon=prefs.horizon()
        )
        assert (await isolated_db.get_listing(listing_id))["role_type"] == "full_time"

        await reclassify_all()
        assert (await isolated_db.get_listing(listing_id))["role_type"] == "full_time"

    def test_an_internship_list_declares_its_postings_internships(self) -> None:
        """"Associate Product Manager" on an internship list is an internship,
        not a senior role - the title's "manager" used to decide it."""
        from roleradar.scout.sources import SOURCES_BY_ID

        assert SOURCES_BY_ID["simplify_intern"].role_type == "internship"
        assert SOURCES_BY_ID["simplify_newgrad"].role_type == "new_grad"
