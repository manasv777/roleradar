"""Shared test fixtures.

The suite makes no network calls and no LLM calls. Anything reaching outward is
either mocked with respx or marked `live` and excluded by default.
"""

from __future__ import annotations

import pytest

from roleradar.db import Database


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every path at a temp directory for the whole suite.

    Not optional and not per-test: a test that monkeypatched `prefs.save_prefs`
    missed the copy the router had already imported by name, so it wrote its
    payload straight into the developer's real data/preferences.json and silently
    reset their configuration. `settings.root` reads ROLERADAR_HOME on each
    access, so setting it here redirects preferences, the database and digests
    for anything that runs under pytest.
    """
    monkeypatch.setenv("ROLERADAR_HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)


@pytest.fixture
async def isolated_db(tmp_path, monkeypatch):
    """Swap the global `db` singleton for a disposable temp-file SQLite DB.

    Tests run against a real database rather than a mock, so persistence, the
    mutable-field guard, and the upsert/corroborate paths are actually
    exercised - without touching the developer's own data.

    A temp *file* rather than `:memory:` is required: SQLite gives each
    connection in the pool its own in-memory database, so separate connections
    would not share state.
    """
    test_db = Database()
    test_db.initialize(tmp_path / "isolated.db")

    # Modules bind the singleton at import time (`from roleradar import db as
    # database`, then `database.db`), so patching the attribute on each module
    # that holds a reference is what actually redirects them.
    import roleradar.db as db_module

    monkeypatch.setattr(db_module, "db", test_db)
    for name in ("roleradar.scout.store", "roleradar.scout.runner"):
        module = __import__(name, fromlist=["database"])
        if hasattr(module, "database"):
            monkeypatch.setattr(module.database, "db", test_db, raising=False)

    try:
        yield test_db
    finally:
        await test_db.close()
