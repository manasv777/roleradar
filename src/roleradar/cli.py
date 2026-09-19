"""Command line interface.

`pyproject.toml` has declared this entry point since the first commit while the
module did not exist, so `pip install` followed by `roleradar` errored out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date

import httpx

from roleradar import db as database
from roleradar.prefs import Preferences, WorkAuth, load_prefs, save_prefs
from roleradar.settings import settings
from roleradar.taxonomy import load_taxonomy


def _api(path: str) -> str:
    return f"http://{settings.host}:{settings.port}/api/v1{path}"


def _server_is_up() -> bool:
    """Is a *roleradar* server answering on our port?

    Checking only for a 200 was wrong: any unrelated API on the same port
    answers that, and the single-writer guard then refuses to run for a reason
    that has nothing to do with roleradar.
    """
    try:
        response = httpx.get(_api("/health"), timeout=2.0)
    except Exception:
        return False
    if response.status_code != 200:
        return False
    try:
        return response.json().get("status") == "ok" and "roleradar" in (
            httpx.get(f"http://{settings.host}:{settings.port}/", timeout=2.0).text
        )
    except Exception:
        return False


# --- init ------------------------------------------------------------------
def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _yes(prompt: str, default: bool = True) -> bool:
    answer = _ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup. Writes data/preferences.json."""
    taxonomy = load_taxonomy()

    if args.defaults:
        prefs = Preferences(
            interests={d.id: [] for d in taxonomy.domains if d.id in ("software", "ai_ml")}
        )
        save_prefs(prefs)
        print(f"wrote defaults to {settings.preferences_path}")
        return 0

    print("\nroleradar setup — pick the fields you want scouted.\n")
    for i, d in enumerate(taxonomy.domains, 1):
        print(f"  {i}. {d.label}")
    picked = _ask("\nDomains (comma-separated numbers, blank = all)")
    chosen = (
        [taxonomy.domains[int(n) - 1] for n in picked.split(",") if n.strip().isdigit()]
        if picked
        else list(taxonomy.domains)
    )

    interests: dict[str, list[str]] = {}
    for d in chosen:
        print(f"\n{d.label} specialties:")
        for i, s in enumerate(d.specialties, 1):
            print(f"  {i}. {s.label}")
        sel = _ask("Specialties (blank = the whole domain)")
        interests[d.id] = [
            d.specialties[int(n) - 1].id for n in sel.split(",") if n.strip().isdigit()
        ]

    role_types = []
    if _yes("\nInclude internships?"):
        role_types.append("internship")
    if _yes("Include new-grad roles?"):
        role_types.append("new_grad")

    full_time_searches: list[str] = []
    if _yes("Include full-time roles too?", default=False):
        role_types.append("full_time")
        from roleradar.prefs import Preferences as _P
        from roleradar.scout.sources.search import search_queries

        suggested = search_queries(_P(interests=interests), taxonomy)
        raw = _ask(
            "Full-time searches (comma-separated, e.g. data engineer, hadoop developer)",
            ", ".join(suggested),
        )
        full_time_searches = [q.strip() for q in raw.split(",") if q.strip()]

    needs_sponsorship = _yes("\nDo you need visa sponsorship?", default=False)

    # Seeded from the chosen specialties so nobody stares at a blank prompt.
    seed = taxonomy.skills_for(
        [f"{d}/{s}" for d, ss in interests.items() for s in (ss or [])]
    )
    if seed:
        print(f"\nSuggested skills from your picks: {', '.join(seed)}")
    skills_raw = _ask("Your skills (comma-separated)", ", ".join(seed))

    prefs = Preferences(
        interests=interests,
        role_types=role_types or ["internship", "new_grad"],
        full_time_searches=full_time_searches,
        work_auth=WorkAuth(requires_sponsorship=needs_sponsorship),
        skills=[s.strip() for s in skills_raw.split(",") if s.strip()],
    )
    path = save_prefs(prefs)
    print(f"\nSaved {path}")
    print(f"Terms in range: {', '.join(t.label for t in prefs.horizon(date.today()))}")
    return 0


# --- run -------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    if args.now:
        try:
            response = httpx.post(_api("/runs"), json={}, timeout=30.0)
        except Exception as exc:
            print(f"could not reach the server: {exc}", file=sys.stderr)
            return 2
        if response.status_code == 409:
            print("a run is already in flight")
            return 0
        print(f"run started: {response.json().get('run_id')}")
        return 0

    # Exactly one process may write the database. Refusing here is cheaper than
    # debugging two runs interleaving on the same SQLite file.
    if _server_is_up():
        print("the server is running — use `roleradar run --now` instead", file=sys.stderr)
        return 2

    from roleradar.scout import runner

    async def _go():
        database.db.initialize(settings.db_path)
        try:
            state = await runner.run_scout()
            print(json.dumps(state.to_dict(), indent=2))
        finally:
            await database.db.close()

    asyncio.run(_go())
    return 0


# --- sources ---------------------------------------------------------------
def cmd_sources(args: argparse.Namespace) -> int:
    from roleradar.scout.sources import SOURCES
    from roleradar.scout.sources.search import search_specs

    failures = 0
    for spec in [*SOURCES, *search_specs(load_prefs(), load_taxonomy())]:
        try:
            response = httpx.head(spec.url, timeout=20.0, follow_redirects=True)
            if response.status_code >= 400:
                response = httpx.get(spec.url, timeout=30.0, follow_redirects=True)
            ok = response.status_code < 400
            size = len(response.content) if response.content else 0
            status = f"{response.status_code}" + (f"  {size:,}B" if size else "")
        except Exception as exc:
            ok, status = False, str(exc)[:60]
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {spec.source_id:32s} {status}")

    # Season-named repos get renamed or archived every year; a feed that 404s
    # silently looks to a new user like the whole tool is broken.
    if failures:
        print(f"\n{failures} source(s) unreachable — edit config or open an issue")
    return 1 if failures and args.strict else 0


# --- reclassify / prune ----------------------------------------------------
def cmd_reclassify(_: argparse.Namespace) -> int:
    from roleradar.scout.reclassify import reclassify_all

    async def _go():
        database.db.initialize(settings.db_path)
        try:
            print(json.dumps(await reclassify_all(), indent=2))
        finally:
            await database.db.close()

    asyncio.run(_go())
    return 0


def cmd_prefs(_: argparse.Namespace) -> int:
    print(json.dumps(load_prefs().to_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roleradar", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="choose your fields and write preferences")
    p_init.add_argument("--defaults", action="store_true", help="skip the questions")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="scout now")
    p_run.add_argument("--now", action="store_true", help="ask a running server to scout")
    p_run.add_argument("--once", action="store_true", help="scout in this process")
    p_run.set_defaults(func=cmd_run)

    p_sources = sub.add_parser("sources", help="check that every feed still answers")
    p_sources.add_argument("--strict", action="store_true", help="exit non-zero on failure")
    p_sources.set_defaults(func=cmd_sources)

    sub.add_parser("reclassify", help="re-judge stored listings").set_defaults(
        func=cmd_reclassify
    )
    sub.add_parser("prefs", help="print current preferences").set_defaults(func=cmd_prefs)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
