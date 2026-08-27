"""Service tests for the enrichment cascade.

The GitHub feeds are a discovery layer only — none of them carry job
descriptions — and no single ATS covers more than a quarter of what they
surface. So enrichment is a cascade: documented JSON APIs first, Workday's
undocumented endpoint second, page rendering last.

Order matters and is asserted here: rendering is a page load, so it must never
run when a cheap JSON tier could have answered.
"""

import httpx
import pytest
import respx

from roleradar.scout.enrich import (
    BoardCache,
    _workday_cxs_url,
    build_enrich_client,
    enrich_listing,
    extract_deadline_from_text,
    html_to_text,
)

pytestmark = pytest.mark.service


def listing(**kwargs: object) -> dict:
    base: dict = {
        "listing_id": "l1",
        "company": "Acme",
        "title": "SWE Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/77",
        "ats_vendor": "greenhouse",
        "ats_tenant": "acme",
        "ats_job_id": "77",
    }
    base.update(kwargs)
    return base


@pytest.fixture
async def client():
    async with build_enrich_client() as c:
        yield c


class TestHtmlToText:
    def test_strips_tags_and_keeps_structure(self) -> None:
        out = html_to_text("<p>Hello</p><ul><li>One</li><li>Two</li></ul>")
        assert "Hello" in out and "One" in out and "<" not in out

    def test_unescapes_entities(self) -> None:
        assert "R&D" in html_to_text("<p>R&amp;D team</p>")

    def test_empty_input(self) -> None:
        assert html_to_text("") == ""


class TestDeadlineExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Applications close November 3, 2026.", "2026-11-03"),
            ("Apply by 2027-01-15 to be considered.", "2027-01-15"),
            ("Application deadline: December 1, 2026", "2026-12-01"),
        ],
    )
    def test_finds_real_deadlines(self, text: str, expected: str) -> None:
        assert extract_deadline_from_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "You thrive in a deadline-driven environment.",
            "Ability to deliver high-quality results under short deadlines.",
            "Willingness to work weekends to meet critical deadlines.",
            "We have a strong deadline culture.",
            "",
        ],
    )
    def test_rejects_prose_without_a_date(self, text: str) -> None:
        """These are the false positives that made body-scanning useless."""
        assert extract_deadline_from_text(text) is None


class TestWorkdayUrlRewrite:
    def test_plain_form(self) -> None:
        got = _workday_cxs_url(
            "https://copart.wd12.myworkdayjobs.com/copart/job/Dallas-TX/SWE-Intern_JR1"
        )
        assert got == (
            "https://copart.wd12.myworkdayjobs.com/wday/cxs/copart/copart/"
            "job/Dallas-TX/SWE-Intern_JR1"
        )

    def test_locale_prefixed_form(self) -> None:
        """Some tenants insert a locale segment before the site."""
        got = _workday_cxs_url(
            "https://asml.wd3.myworkdayjobs.com/en-US/asmlext1/job/San-Jose/SDET_J-1"
        )
        assert got == (
            "https://asml.wd3.myworkdayjobs.com/wday/cxs/asml/asmlext1/"
            "job/San-Jose/SDET_J-1"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://acme.wd1.myworkdayjobs.com/careers",  # no /job/ segment
            "",
            "not a url",
        ],
    )
    def test_returns_none_for_unusable_urls(self, url: str) -> None:
        assert _workday_cxs_url(url) is None


class TestCascade:
    @respx.mock
    async def test_json_tier_answers_without_rendering(self, client, monkeypatch) -> None:
        """Rendering is a page load — it must not run when JSON can answer."""
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(
                200,
                json={"jobs": [{"id": 77, "content": "<p>" + "Python work. " * 40 + "</p>"}]},
            )
        )
        rendered = {"called": False}

        async def never(_listing):
            rendered["called"] = True
            return None

        monkeypatch.setattr("roleradar.scout.enrich._tier_render", never)

        result = await enrich_listing(listing(), client=client, cache=BoardCache())

        assert result.ok and result.tier == "ats_json"
        assert rendered["called"] is False

    @respx.mock
    async def test_falls_through_to_render_when_json_declines(
        self, client, monkeypatch
    ) -> None:
        from roleradar.scout.enrich import Enrichment

        async def rendered(_listing):
            return Enrichment(
                description_text="x" * 900, still_listed=True, tier="rendered"
            )

        monkeypatch.setattr("roleradar.scout.enrich._tier_render", rendered)

        # A direct career site: no supported JSON API at all.
        result = await enrich_listing(
            listing(ats_vendor="other", ats_tenant=None, ats_job_id=None,
                    apply_url="https://careers.example.com/jobs/1"),
            client=client,
            cache=BoardCache(),
        )
        assert result.ok and result.tier == "rendered"

    @respx.mock
    async def test_delisted_job_is_reported_not_rendered(
        self, client, monkeypatch
    ) -> None:
        """A confirmed delisting is an answer — don't spend a page load on it."""
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": 999, "content": "x"}]})
        )
        rendered = {"called": False}

        async def never(_listing):
            rendered["called"] = True
            return None

        monkeypatch.setattr("roleradar.scout.enrich._tier_render", never)

        result = await enrich_listing(listing(), client=client, cache=BoardCache())

        assert result.ok is False
        assert result.still_listed is False
        assert rendered["called"] is False

    @respx.mock
    async def test_board_is_fetched_once_per_tenant(self, client) -> None:
        """One board call must cover every listing at that company."""
        route = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": 77, "content": "<p>" + "A " * 400 + "</p>"},
                        {"id": 78, "content": "<p>" + "B " * 400 + "</p>"},
                    ]
                },
            )
        )
        cache = BoardCache()
        await enrich_listing(listing(ats_job_id="77"), client=client, cache=cache)
        await enrich_listing(listing(ats_job_id="78"), client=client, cache=cache)

        assert route.call_count == 1

    @respx.mock
    async def test_render_disabled_yields_a_clean_failure(self, client) -> None:
        result = await enrich_listing(
            listing(ats_vendor="other", ats_tenant=None, ats_job_id=None),
            client=client,
            cache=BoardCache(),
            allow_render=False,
        )
        assert result.ok is False
        assert "no description obtainable" in (result.error or "")

    @respx.mock
    async def test_board_fetch_failure_never_raises(self, client, monkeypatch) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            side_effect=httpx.ConnectError("boom")
        )

        async def never(_listing):
            return None

        monkeypatch.setattr("roleradar.scout.enrich._tier_render", never)

        result = await enrich_listing(listing(), client=client, cache=BoardCache())
        assert result.ok is False
