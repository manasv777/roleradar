"""Unit tests for Scout feed normalization.

Pure functions, no network, no LLM. The fixture holds real records captured
from the live SimplifyJobs feed so the shape assertions track reality.
"""

import json
from pathlib import Path

import pytest

from roleradar.scout.normalize import (
    ScoutRecord,
    canonical_url,
    detect_ats,
    epoch_to_iso,
    fingerprint,
    normalize_simplify,
    normalize_vansh,
    normalize_zshah,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "scout" / "simplify_listings.json"


@pytest.fixture(scope="module")
def simplify_records() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestEpochToIso:
    def test_converts_seconds(self) -> None:
        assert epoch_to_iso(1769182182).startswith("2026-01-23T")

    def test_converts_milliseconds(self) -> None:
        """Some feeds emit ms; both must land on the same date."""
        assert epoch_to_iso(1769182182000)[:10] == epoch_to_iso(1769182182)[:10]

    @pytest.mark.parametrize("value", [None, 0, -1, "", "abc", True, False, {}])
    def test_rejects_non_timestamps(self, value: object) -> None:
        """0 is a null sentinel in these feeds, and booleans are not epochs."""
        assert epoch_to_iso(value) is None

    def test_accepts_numeric_string(self) -> None:
        assert epoch_to_iso("1769182182") == epoch_to_iso(1769182182)


class TestCanonicalUrl:
    def test_strips_tracking_and_fragment(self) -> None:
        got = canonical_url(
            "https://boards.greenhouse.io/acme/jobs/1?utm_source=simplify&gh_src=x&keep=1#top"
        )
        assert got == "https://boards.greenhouse.io/acme/jobs/1?keep=1"

    def test_lowercases_host_only(self) -> None:
        """Paths can be case-sensitive; hosts are not."""
        got = canonical_url("https://Boards.Greenhouse.IO/AcmeCo/jobs/1")
        assert got == "https://boards.greenhouse.io/AcmeCo/jobs/1"

    def test_strips_trailing_slash(self) -> None:
        assert canonical_url("https://a.co/x/") == canonical_url("https://a.co/x")

    def test_empty_input(self) -> None:
        assert canonical_url("") == ""


class TestFingerprint:
    def test_stable_across_tracking_params(self) -> None:
        """The same role from two feeds must collapse to one fingerprint."""
        a = fingerprint("Acme", "SWE Intern", "https://a.co/j/1?utm_source=simplify")
        b = fingerprint("Acme", "SWE Intern", "https://a.co/j/1?utm_source=vansh")
        assert a == b

    def test_insensitive_to_case_and_spacing(self) -> None:
        a = fingerprint("Acme  Corp", "SWE   Intern", "https://a.co/j/1")
        b = fingerprint("ACME CORP", "swe intern", "https://a.co/j/1")
        assert a == b

    def test_distinguishes_different_roles(self) -> None:
        a = fingerprint("Acme", "SWE Intern", "https://a.co/j/1")
        b = fingerprint("Acme", "ML Intern", "https://a.co/j/2")
        assert a != b


class TestDetectAts:
    @pytest.mark.parametrize(
        "url,vendor,tenant",
        [
            ("https://boards.greenhouse.io/spacex/jobs/8403", "greenhouse", "spacex"),
            ("https://job-boards.greenhouse.io/acme/jobs/99", "greenhouse", "acme"),
            ("https://jobs.ashbyhq.com/ramp/abc-123", "ashby", "ramp"),
            ("https://jobs.lever.co/palantir/xyz", "lever", "palantir"),
            ("https://haier.wd3.myworkdayjobs.com/ge/job/X", "workday", "haier"),
            ("https://jobs.smartrecruiters.com/BoschGroup/7", "smartrecruiters", "BoschGroup"),
        ],
    )
    def test_known_vendors(self, url: str, vendor: str, tenant: str) -> None:
        got_vendor, got_tenant, _ = detect_ats(url)
        assert (got_vendor, got_tenant) == (vendor, tenant)

    def test_greenhouse_job_id(self) -> None:
        assert detect_ats("https://boards.greenhouse.io/spacex/jobs/8403")[2] == "8403"

    def test_unknown_host(self) -> None:
        assert detect_ats("https://careers.example.com/apply/7")[0] == "other"

    @pytest.mark.parametrize("url", ["", None])
    def test_empty_url(self, url: str | None) -> None:
        assert detect_ats(url or "") == (None, None, None)


class TestNormalizeSimplify:
    def test_every_fixture_record_normalizes(self, simplify_records: list[dict]) -> None:
        for record in simplify_records:
            result = normalize_simplify(record, "simplify_intern")
            assert isinstance(result, ScoutRecord), record.get("id")
            assert result.company and result.title and result.apply_url

    def test_epoch_fields_become_iso(self, simplify_records: list[dict]) -> None:
        for record in simplify_records:
            result = normalize_simplify(record, "simplify_intern")
            assert result is not None
            for value in (result.date_posted, result.date_updated):
                # ISO-8601, not the raw epoch integer it came from.
                assert value is None or value[4] == "-"

    def test_drops_na_sentinel_from_terms(self) -> None:
        record = {"id": "1", "company_name": "A", "title": "T", "url": "https://a.co", "terms": ["N/A"]}
        result = normalize_simplify(record, "s")
        assert result is not None and result.terms == []

    def test_dedupes_repeated_terms(self) -> None:
        """One live NBCUniversal record listed the same term twice."""
        record = {
            "id": "1", "company_name": "A", "title": "T", "url": "https://a.co",
            "terms": ["Spring 2027", "spring 2027", "Spring 2027"],
        }
        result = normalize_simplify(record, "s")
        assert result is not None and result.terms == ["Spring 2027"]

    def test_detects_remote_from_location(self) -> None:
        record = {
            "id": "1", "company_name": "A", "title": "T", "url": "https://a.co",
            "locations": ["Remote"],
        }
        result = normalize_simplify(record, "s")
        assert result is not None and result.is_remote is True

    def test_hidden_record_is_dropped(self) -> None:
        record = {
            "id": "1", "company_name": "A", "title": "T", "url": "https://a.co",
            "is_visible": False,
        }
        assert normalize_simplify(record, "s") is None

    @pytest.mark.parametrize(
        "record",
        [
            {},
            {"id": "1"},
            {"id": "1", "company_name": "A"},
            {"id": "1", "company_name": "A", "title": "T"},  # no url
            {"company_name": "A", "title": "T", "url": "https://a.co"},  # no id
        ],
    )
    def test_incomplete_records_return_none(self, record: dict) -> None:
        assert normalize_simplify(record, "s") is None

    @pytest.mark.parametrize("record", [None, "a string", 42, []])
    def test_malformed_input_never_raises(self, record: object) -> None:
        """One bad row must not abort a 14k-record run."""
        assert normalize_simplify(record, "s") is None  # type: ignore[arg-type]


class TestNormalizeVansh:
    def test_bare_season_yields_no_terms(self) -> None:
        """`season` has no year, so it cannot bucket anything on its own."""
        record = {
            "id": "1", "company_name": "SpaceX", "title": "Eng Intern",
            "url": "https://a.co", "season": "Fall",
        }
        result = normalize_vansh(record, "vansh")
        assert result is not None and result.terms == []

    def test_malformed_input_never_raises(self) -> None:
        assert normalize_vansh({}, "vansh") is None


class TestNormalizeZshah:
    def test_season_with_year_becomes_a_term(self) -> None:
        record = {
            "id": "1", "company": "Acme", "title": "SWE Intern",
            "url": "https://a.co", "season": "Summer 2027", "remote": True,
        }
        result = normalize_zshah(record, "zshah")
        assert result is not None
        assert result.terms == ["Summer 2027"] and result.is_remote is True

    def test_not_stated_season_is_dropped(self) -> None:
        record = {
            "id": "1", "company": "Acme", "title": "SWE Intern",
            "url": "https://a.co", "season": "Not stated",
        }
        result = normalize_zshah(record, "zshah")
        assert result is not None and result.terms == []

    def test_malformed_input_never_raises(self) -> None:
        assert normalize_zshah({}, "zshah") is None
