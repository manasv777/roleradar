import pytest

pytest.skip(
    "Router phase pending: this suite drives the FastAPI app, which roleradar "
    "does not assemble yet. Kept in place, unmodified apart from the rename "
    "map, so it becomes the acceptance gate for the API work rather than "
    "something rewritten to match whatever gets built.",
    allow_module_level=True,
)

"""Integration tests for the Scout API, over real routers and a temp database."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from roleradar.scout import runner as runner_mod

pytestmark = pytest.mark.integration

BASE = "/api/v1/scout"


@pytest.fixture(autouse=True)
def _reset_runner():
    runner_mod._current_state = None
    runner_mod._current_task = None
    yield
    runner_mod._current_state = None
    runner_mod._current_task = None


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(isolated_db, **overrides) -> dict:
    row = {
        "source_id": "simplify_intern",
        "external_id": "x1",
        "fingerprint": "fp1",
        "company": "Acme",
        "title": "Software Engineer Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "bucket": "spring_2027",
        "bucket_confidence": 0.95,
        "bucket_reasons": ["single-term listing: spring 2027"],
        "track": "swe",
    }
    row.update(overrides)
    return await isolated_db.create_listing(row)


class TestListListings:
    async def test_returns_listings_with_bucket_counts(self, isolated_db) -> None:
        await _seed(isolated_db)
        await _seed(
            isolated_db,
            external_id="x2",
            fingerprint="fp2",
            bucket="summer_2027",
            title="ML Intern",
        )

        async with _client() as client:
            response = await client.get(f"{BASE}/listings")

        assert response.status_code == 200
        body = response.json()
        assert len(body["listings"]) == 2
        assert body["counts"]["spring_2027"] == 1
        assert body["counts"]["summer_2027"] == 1
        assert body["total"] == 2

    async def test_bucket_filter(self, isolated_db) -> None:
        await _seed(isolated_db)
        await _seed(
            isolated_db, external_id="x2", fingerprint="fp2", bucket="summer_2027"
        )

        async with _client() as client:
            response = await client.get(f"{BASE}/listings", params={"bucket": "summer_2027"})

        assert [r["bucket"] for r in response.json()["listings"]] == ["summer_2027"]

    async def test_verification_flag_is_in_the_contract(self, isolated_db) -> None:
        """Clients must be able to see that a row is unverified, and why."""
        await _seed(
            isolated_db,
            bucket="fall_2026_no_auth",
            needs_verification=True,
            bucket_reasons=["all locations outside the US: Toronto, ON, Canada"],
        )

        async with _client() as client:
            body = (await client.get(f"{BASE}/listings")).json()

        row = body["listings"][0]
        assert row["needs_verification"] is True
        assert row["bucket_reasons"]

    async def test_dismissed_hidden_by_default(self, isolated_db) -> None:
        await _seed(isolated_db, dismissed=True)

        async with _client() as client:
            hidden = (await client.get(f"{BASE}/listings")).json()
            shown = (
                await client.get(f"{BASE}/listings", params={"include_dismissed": True})
            ).json()

        assert hidden["listings"] == []
        assert len(shown["listings"]) == 1

    async def test_confidence_floor(self, isolated_db) -> None:
        await _seed(isolated_db, bucket_confidence=0.35)

        async with _client() as client:
            response = await client.get(f"{BASE}/listings", params={"min_confidence": 0.5})

        assert response.json()["listings"] == []


class TestListingDetail:
    async def test_detail_includes_description_and_terms(self, isolated_db) -> None:
        seeded = await _seed(
            isolated_db,
            description_text="Python and FastAPI internship.",
            raw_terms=["Spring 2027"],
            ats_vendor="greenhouse",
        )

        async with _client() as client:
            response = await client.get(f"{BASE}/listings/{seeded['listing_id']}")

        body = response.json()
        assert body["description_text"].startswith("Python")
        assert body["raw_terms"] == ["Spring 2027"]

    async def test_unknown_listing_is_404(self, isolated_db) -> None:
        async with _client() as client:
            assert (await client.get(f"{BASE}/listings/nope")).status_code == 404


class TestUpdateListing:
    async def test_manual_deadline_is_stamped_manual(self, isolated_db) -> None:
        seeded = await _seed(isolated_db)

        async with _client() as client:
            response = await client.patch(
                f"{BASE}/listings/{seeded['listing_id']}", json={"deadline": "2027-01-15"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["deadline"] == "2027-01-15"
        assert body["deadline_source"] == "manual"

    async def test_dismiss(self, isolated_db) -> None:
        seeded = await _seed(isolated_db)

        async with _client() as client:
            response = await client.patch(
                f"{BASE}/listings/{seeded['listing_id']}", json={"dismissed": True}
            )

        assert response.json()["dismissed"] is True

    async def test_empty_payload_is_rejected(self, isolated_db) -> None:
        seeded = await _seed(isolated_db)

        async with _client() as client:
            response = await client.patch(f"{BASE}/listings/{seeded['listing_id']}", json={})

        assert response.status_code == 400


class TestRun:
    async def test_run_returns_immediately_with_an_id(self, isolated_db) -> None:
        """A full run takes hours; the request must not wait for it."""
        import asyncio

        gate = asyncio.Event()

        async def blocked(*_a, **_k):
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=blocked):
            async with _client() as client:
                response = await client.post(f"{BASE}/run", json={})
                assert response.status_code == 202
                assert response.json()["run_id"]

                # A second request must not start an overlapping run.
                conflict = await client.post(f"{BASE}/run", json={})
                assert conflict.status_code == 409
            gate.set()
            await runner_mod.cancel_scout_run()

    async def test_run_state_is_pollable(self, isolated_db) -> None:
        import asyncio

        gate = asyncio.Event()

        async def blocked(*_a, **_k):
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=blocked):
            async with _client() as client:
                started = (await client.post(f"{BASE}/run", json={})).json()
                polled = (await client.get(f"{BASE}/run")).json()
                assert polled["run_id"] == started["run_id"]
            gate.set()
            await runner_mod.cancel_scout_run()

    async def test_run_state_is_null_before_any_run(self, isolated_db) -> None:
        async with _client() as client:
            assert (await client.get(f"{BASE}/run")).json() is None


class TestSources:
    async def test_configured_sources_appear_before_any_fetch(self, isolated_db) -> None:
        """A feed that has never run still belongs in the freshness view."""
        async with _client() as client:
            body = (await client.get(f"{BASE}/sources")).json()

        ids = {row["source_id"] for row in body}
        assert "simplify_intern" in ids
        assert any(row["note"] for row in body), "licensing notes must be surfaced"

    async def test_error_state_is_visible(self, isolated_db) -> None:
        """A dead feed must be visible — an empty run looks like a quiet day."""
        await isolated_db.upsert_source(
            "simplify_intern",
            "https://example.test/f.json",
            {"last_status": "error", "last_error": "HTTP 404 — feed may have moved"},
        )

        async with _client() as client:
            body = (await client.get(f"{BASE}/sources")).json()

        row = next(r for r in body if r["source_id"] == "simplify_intern")
        assert row["last_status"] == "error"
        assert "404" in row["last_error"]


class TestDraftEndpoint:
    async def test_missing_master_resume_is_a_clear_400(self, isolated_db) -> None:
        seeded = await _seed(isolated_db, description_text="A real JD. " * 50)

        async with _client() as client:
            response = await client.post(f"{BASE}/listings/{seeded['listing_id']}/draft")

        assert response.status_code == 400
        assert "master resume" in response.json()["detail"]


class TestRunOptions:
    async def test_reporting_and_window_options_reach_the_runner(
        self, isolated_db
    ) -> None:
        """Alerting and quiet hours must be controllable from the API."""
        import asyncio

        captured: dict = {}
        gate = asyncio.Event()

        async def capture(*_a, **kwargs):
            captured.update(kwargs)
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=capture):
            async with _client() as client:
                response = await client.post(
                    f"{BASE}/run",
                    json={
                        "notify": False,
                        "digest": False,
                        "respect_backlog_window": False,
                        "draft_cap": 2,
                    },
                )
                assert response.status_code == 202
            gate.set()
            await runner_mod.cancel_scout_run()

        assert captured["notify"] is False
        assert captured["digest"] is False
        assert captured["respect_backlog_window"] is False
        assert captured["draft_cap"] == 2

    async def test_defaults_drain_and_report(self, isolated_db) -> None:
        """No body means: drain the queue, respect quiet hours, tell me."""
        import asyncio

        captured: dict = {}
        gate = asyncio.Event()

        async def capture(*_a, **kwargs):
            captured.update(kwargs)
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=capture):
            async with _client() as client:
                await client.post(f"{BASE}/run", json={})
            gate.set()
            await runner_mod.cancel_scout_run()

        assert captured["draft_cap"] is None, "default is drain, not a fixed cap"
        assert captured["respect_backlog_window"] is True
        assert captured["notify"] is True and captured["digest"] is True

    async def test_run_state_exposes_reporting_fields(self, isolated_db) -> None:
        import asyncio

        gate = asyncio.Event()

        async def blocked(*_a, **_k):
            await gate.wait()

        with patch.object(runner_mod, "run_scout", side_effect=blocked):
            async with _client() as client:
                await client.post(f"{BASE}/run", json={})
                body = (await client.get(f"{BASE}/run")).json()
            gate.set()
            await runner_mod.cancel_scout_run()

        assert "notifications_sent" in body
        assert "digest_path" in body
        assert "enriched_by_tier" in body
