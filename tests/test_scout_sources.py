"""Service tests for Scout feed fetching.

Network is mocked with respx — the default suite makes no real requests.

The 304 path gets its own test because it is the whole point of the design: the
primary feed is ~10 MB and republished every 30 minutes, so a run that ignored
conditional-GET would re-download it every single day for nothing.
"""

import json

import httpx
import pytest
import respx

from roleradar.scout.normalize import normalize_simplify
from roleradar.scout.sources import (
    SourceSpec,
    build_client,
    fetch_all,
    fetch_source,
)

pytestmark = pytest.mark.service

FEED_URL = "https://example.test/listings.json"

SPEC = SourceSpec(
    source_id="test_feed",
    url=FEED_URL,
    normalizer=normalize_simplify,
)

SAMPLE = [
    {
        "id": "a1",
        "company_name": "Acme",
        "title": "Software Engineer Intern",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "terms": ["Spring 2027"],
        "category": "Software",
        "active": True,
        "locations": ["NYC"],
        "date_posted": 1769182182,
    },
    {
        "id": "a2",
        "company_name": "Globex",
        "title": "ML Intern",
        "url": "https://jobs.lever.co/globex/2",
        "terms": ["Summer 2027"],
        "category": "AI/ML/Data",
        "active": True,
        "locations": ["Remote"],
        "date_posted": 1769182182,
    },
    # Malformed: missing url. Must be skipped, not fatal.
    {"id": "a3", "company_name": "Initech", "title": "Intern"},
]


@pytest.fixture
async def client():
    async with build_client() as c:
        yield c


class TestFetchSource:
    @respx.mock
    async def test_successful_fetch_normalizes_records(self, client) -> None:
        respx.get(FEED_URL).mock(
            return_value=httpx.Response(
                200, json=SAMPLE, headers={"ETag": 'W/"v1"', "Last-Modified": "Mon, 01 Jan 2027 00:00:00 GMT"}
            )
        )
        result = await fetch_source(SPEC, client=client)

        assert result.status == "ok"
        assert len(result.records) == 2, "the malformed record must be skipped"
        assert result.raw_count == 3
        assert result.etag == 'W/"v1"'
        assert result.records[0].company == "Acme"

    @respx.mock
    async def test_sends_conditional_headers_and_handles_304(self, client) -> None:
        """The core efficiency property: unchanged upstream transfers no body."""
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(304))
        result = await fetch_source(
            SPEC, client=client, etag='W/"v1"', last_modified="Mon, 01 Jan 2027 00:00:00 GMT"
        )

        assert result.status == "not_modified"
        assert result.records == []
        # The stored validators are carried forward, not discarded.
        assert result.etag == 'W/"v1"'

        sent = route.calls.last.request
        assert sent.headers["If-None-Match"] == 'W/"v1"'
        assert sent.headers["If-Modified-Since"] == "Mon, 01 Jan 2027 00:00:00 GMT"

    @respx.mock
    async def test_omits_conditional_headers_on_first_fetch(self, client) -> None:
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=[]))
        await fetch_source(SPEC, client=client)

        sent = route.calls.last.request
        assert "If-None-Match" not in sent.headers
        assert "If-Modified-Since" not in sent.headers

    @respx.mock
    async def test_404_fails_immediately_without_retry(self, client) -> None:
        """A renamed feed repo must surface loudly, not as an empty success.

        This is the dangerous failure mode: a silently empty run looks
        identical to 'nothing new today'.
        """
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(404))
        result = await fetch_source(SPEC, client=client)

        assert result.status == "error"
        assert "404" in (result.error or "")
        assert route.call_count == 1, "4xx must not be retried"

    @respx.mock
    async def test_500_is_retried_then_reported(self, client) -> None:
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(500))
        result = await fetch_source(SPEC, client=client)

        assert result.status == "error"
        assert route.call_count == 3, "one initial attempt plus two retries"

    @respx.mock
    async def test_transient_500_then_success(self, client) -> None:
        respx.get(FEED_URL).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json=SAMPLE)]
        )
        result = await fetch_source(SPEC, client=client)

        assert result.status == "ok" and len(result.records) == 2

    @respx.mock
    async def test_malformed_json_is_an_error_not_an_exception(self, client) -> None:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text="{not json"))
        result = await fetch_source(SPEC, client=client)

        assert result.status == "error"
        assert "malformed" in (result.error or "")

    @respx.mock
    async def test_payload_that_is_not_a_list_is_an_error(self, client) -> None:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))
        result = await fetch_source(SPEC, client=client)

        assert result.status == "error"

    @respx.mock
    async def test_transport_error_is_retried_then_reported(self, client) -> None:
        route = respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("boom"))
        result = await fetch_source(SPEC, client=client)

        assert result.status == "error"
        assert "transport error" in (result.error or "")
        assert route.call_count == 3

    @respx.mock
    async def test_root_key_unwrapping(self, client) -> None:
        wrapped = SourceSpec(
            source_id="wrapped", url=FEED_URL, normalizer=normalize_simplify, root_key="jobs"
        )
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"jobs": SAMPLE}))
        result = await fetch_source(wrapped, client=client)

        assert result.status == "ok" and len(result.records) == 2

    @respx.mock
    async def test_identifying_user_agent_is_sent(self, client) -> None:
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=[]))
        await fetch_source(SPEC, client=client)

        assert "resume-matcher-scout" in route.calls.last.request.headers["User-Agent"]


class TestFetchAll:
    @respx.mock
    async def test_disabled_source_is_skipped(self, client) -> None:
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=SAMPLE))
        results = await fetch_all(
            [SPEC], source_state={"test_feed": {"enabled": False}}, client=client
        )

        assert results[0].status == "disabled"
        assert route.call_count == 0

    @respx.mock
    async def test_source_off_by_default_is_skipped_when_unseen(self, client) -> None:
        opt_in = SourceSpec(
            source_id="opt_in",
            url=FEED_URL,
            normalizer=normalize_simplify,
            enabled_by_default=False,
        )
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=SAMPLE))
        results = await fetch_all([opt_in], source_state={}, client=client)

        assert results[0].status == "disabled"
        assert route.call_count == 0

    @respx.mock
    async def test_one_dead_source_does_not_abort_the_run(self, client) -> None:
        """A broken feed must degrade the run, not end it."""
        good_url = "https://example.test/good.json"
        good = SourceSpec(source_id="good", url=good_url, normalizer=normalize_simplify)
        respx.get(FEED_URL).mock(return_value=httpx.Response(404))
        respx.get(good_url).mock(return_value=httpx.Response(200, json=SAMPLE))

        results = await fetch_all([SPEC, good], source_state={}, client=client)

        assert [r.status for r in results] == ["error", "ok"]
        assert len(results[1].records) == 2

    @respx.mock
    async def test_replays_stored_validators_per_source(self, client) -> None:
        route = respx.get(FEED_URL).mock(return_value=httpx.Response(304))
        await fetch_all(
            [SPEC], source_state={"test_feed": {"etag": 'W/"abc"'}}, client=client
        )

        assert route.calls.last.request.headers["If-None-Match"] == 'W/"abc"'


class TestRealFeedShape:
    """Guards the fixture against upstream schema drift."""

    def test_fixture_records_match_the_normalizer_contract(self) -> None:
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "scout" / "simplify_listings.json"
        )
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        assert raw, "fixture must not be empty"
        for item in raw:
            record = normalize_simplify(item, "simplify_intern")
            assert record is not None, item.get("id")
