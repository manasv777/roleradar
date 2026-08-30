"""API integration tests, over the real routers and an isolated database."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from roleradar.app import create_app
from roleradar.scout import runner as runner_mod

pytestmark = pytest.mark.integration

BASE = "/api/v1"


@pytest.fixture(autouse=True)
def _reset_runner():
    runner_mod._current_state = None
    runner_mod._current_task = None
    yield
    runner_mod._current_state = None
    runner_mod._current_task = None


@pytest.fixture
async def client(isolated_db, monkeypatch):
    """An ASGI client whose routers point at the isolated database."""
    import roleradar.routers.scout as router_mod

    monkeypatch.setattr(router_mod, "db", isolated_db)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed(db, **overrides) -> dict:
    row = {
        "source_id": "simplify_intern",
        "external_id": "x1",
        "fingerprint": "fp1",
        "company": "Acme",
        "title": "Software Engineer Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "term_id": "spring_2027",
        "role_type": "internship",
        "classify_confidence": 0.95,
        "classify_reasons": ["single-term listing"],
    }
    row.update(overrides)
    created = await db.create_listing(row)
    await db.set_labels(
        created["listing_id"],
        {"domain": ["software"], "specialty": ["software/backend"]},
    )
    return created


class TestListings:
    async def test_empty_database_is_not_an_error(self, client) -> None:
        response = await client.get(f"{BASE}/listings")
        assert response.status_code == 200
        assert response.json()["listings"] == []

    async def test_labels_are_returned(self, client, isolated_db) -> None:
        await _seed(isolated_db)
        row = (await client.get(f"{BASE}/listings")).json()["listings"][0]
        assert row["domains"] == ["software"]
        assert row["specialties"] == ["software/backend"]

    async def test_specialty_filter(self, client, isolated_db) -> None:
        await _seed(isolated_db)
        hit = await client.get(f"{BASE}/listings", params={"specialty": "software/backend"})
        miss = await client.get(f"{BASE}/listings", params={"specialty": "quant/trading"})
        assert len(hit.json()["listings"]) == 1
        assert miss.json()["listings"] == []

    async def test_total_reflects_the_filters(self, client, isolated_db) -> None:
        """The count and the list must agree, or pagination offers empty pages."""
        await _seed(isolated_db, external_id="a", fingerprint="fa")
        await _seed(isolated_db, external_id="b", fingerprint="fb", role_type="new_grad")

        everything = (await client.get(f"{BASE}/listings")).json()
        interns = (
            await client.get(f"{BASE}/listings", params={"role_type": "internship"})
        ).json()

        assert everything["total"] == 2
        assert interns["total"] == 1 == len(interns["listings"])

    async def test_pagination_offsets(self, client, isolated_db) -> None:
        for i in range(5):
            await _seed(isolated_db, external_id=f"e{i}", fingerprint=f"f{i}")
        page = (
            await client.get(f"{BASE}/listings", params={"limit": 2, "offset": 4})
        ).json()
        assert page["total"] == 5
        assert len(page["listings"]) == 1     # the remainder, not a full page

    async def test_detail_and_missing(self, client, isolated_db) -> None:
        created = await _seed(isolated_db)
        ok = await client.get(f"{BASE}/listings/{created['listing_id']}")
        assert ok.status_code == 200
        assert (await client.get(f"{BASE}/listings/nope")).status_code == 404


class TestUpdate:
    async def test_dismissal_persists(self, client, isolated_db) -> None:
        created = await _seed(isolated_db)
        response = await client.patch(
            f"{BASE}/listings/{created['listing_id']}", json={"dismissed": True}
        )
        assert response.status_code == 200 and response.json()["dismissed"] is True

    async def test_a_typed_deadline_is_marked_manual(self, client, isolated_db) -> None:
        """So a later parse cannot quietly overwrite what the user typed."""
        created = await _seed(isolated_db)
        response = await client.patch(
            f"{BASE}/listings/{created['listing_id']}", json={"deadline": "2027-01-15"}
        )
        assert response.json()["deadline_source"] == "manual"

    async def test_empty_update_is_rejected(self, client, isolated_db) -> None:
        created = await _seed(isolated_db)
        response = await client.patch(f"{BASE}/listings/{created['listing_id']}", json={})
        assert response.status_code == 400


class TestConfigEndpoints:
    async def test_terms_are_computed_not_stored(self, client) -> None:
        terms = (await client.get(f"{BASE}/terms")).json()
        assert terms and all({"id", "label", "season", "year"} <= set(t) for t in terms)

    async def test_taxonomy_lists_domains_and_specialties(self, client) -> None:
        body = (await client.get(f"{BASE}/taxonomy")).json()
        assert body["version"] >= 1
        assert any(d["specialties"] for d in body["domains"])

    async def test_preferences_round_trip(self, client, tmp_path, monkeypatch) -> None:
        import roleradar.prefs as prefs_mod

        monkeypatch.setattr(
            prefs_mod, "save_prefs", lambda p, path=None: tmp_path / "p.json"
        )
        payload = {"interests": {"software": ["backend"]}, "skills": ["python"]}
        response = await client.put(f"{BASE}/preferences", json=payload)
        assert response.status_code == 200
        assert response.json()["interests"] == {"software": ["backend"]}


class TestRuns:
    async def test_no_run_yet_returns_null(self, client) -> None:
        assert (await client.get(f"{BASE}/runs/current")).json() is None

    async def test_health(self, client) -> None:
        assert (await client.get(f"{BASE}/health")).json()["status"] == "ok"


class TestSources:
    async def test_never_fetched_sources_are_still_listed(self, client) -> None:
        """A feed that has failed since day one is the one worth surfacing."""
        rows = (await client.get(f"{BASE}/sources")).json()
        assert rows and all("source_id" in r for r in rows)
