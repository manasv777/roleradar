"""The scout run loop: fetch → classify → store → enrich → advise → report.

A single in-process asyncio task guarded by a module-level lock — the same
idiom as ``Database._master_resume_lock``. No queue, no scheduler library, no
second process, because the whole app assumes one uvicorn worker and a second
writer would break the caches and locks that assumption buys.

**This is a new pattern in this codebase** — nothing else here spawns a
background task — so two things are handled explicitly that would otherwise
bite silently: an uncaught exception in a bare ``create_task`` vanishes without
a done-callback, and a shutdown mid-run orphans half-written state unless the
task is cancelled during lifespan teardown.

On pacing: drafting drains the backlog by default rather than stopping at a
fixed count. The count was never the real constraint — the 409 lock already
prevents overlapping runs, so a long run simply makes the next trigger a no-op.
What genuinely needs bounding is wall-clock (so a run can be told to stop before
the workday) and failure (so a dead Ollama does not burn hours retrying).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from roleradar import db as database
from roleradar.prefs import load_prefs
from roleradar.taxonomy import load_taxonomy
from roleradar.scout import advise as advise_mod
from roleradar.scout import enrich as enrich_mod
from roleradar.scout.sources import SOURCES, build_client, fetch_source
from roleradar.scout.store import (
    UpsertStats,
    load_source_state,
    save_source_state,
    upsert_records,
)

logger = logging.getLogger(__name__)

FRESH_LISTING_HOURS = 24

class ScoutAlreadyRunning(RuntimeError):
    """Raised when a run is requested while one is already in flight."""


@dataclass
class RunState:
    """Live progress for one run. Polled by the UI and the launchd script."""

    run_id: str
    started_at: str
    phase: str = "queued"
    finished_at: str | None = None
    sources_ok: int = 0
    sources_not_modified: int = 0
    sources_error: int = 0
    listings_new: int = 0
    listings_updated: int = 0
    listings_corroborated: int = 0
    listings_deactivated: int = 0
    enriched_ok: int = 0
    enriched_failed: int = 0
    enriched_by_tier: dict[str, int] = field(default_factory=dict)
    advised: int = 0
    notifications_sent: int = 0
    digest_path: str | None = None
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "phase": self.phase,
            "sources_ok": self.sources_ok,
            "sources_not_modified": self.sources_not_modified,
            "sources_error": self.sources_error,
            "listings_new": self.listings_new,
            "listings_updated": self.listings_updated,
            "listings_corroborated": self.listings_corroborated,
            "listings_deactivated": self.listings_deactivated,
            "enriched_ok": self.enriched_ok,
            "enriched_failed": self.enriched_failed,
            "enriched_by_tier": self.enriched_by_tier,
            "advised": self.advised,
            "notifications_sent": self.notifications_sent,
            "digest_path": self.digest_path,
            "errors": self.errors,
            "cancelled": self.cancelled,
        }


_run_lock = asyncio.Lock()
_current_state: RunState | None = None
_current_task: asyncio.Task | None = None


def get_run_state() -> RunState | None:
    """The in-flight run, or the most recent finished one."""
    return _current_state


def is_running() -> bool:
    return _run_lock.locked()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- phases ---------------------------------------------------------------


async def _phase_fetch_and_store(state: RunState) -> None:
    """Fetch every enabled feed, classify, and persist the in-scope subset."""
    # Loaded once per run: the taxonomy compiles regexes, and re-reading
    # preferences per record would let them change mid-run.
    prefs = load_prefs()
    taxonomy = load_taxonomy()
    horizon = prefs.horizon()
    state.phase = "fetching"
    source_state = await load_source_state()
    totals = UpsertStats()

    async with build_client() as client:
        for index, spec in enumerate(SOURCES):
            stored = source_state.get(spec.source_id) or {}
            if stored.get("enabled") is False or (
                not stored and not spec.enabled_by_default
            ):
                continue
            if index > 0:
                await asyncio.sleep(1.0)

            result = await fetch_source(
                spec,
                client=client,
                etag=stored.get("etag"),
                last_modified=stored.get("last_modified"),
            )

            if result.status == "ok":
                state.sources_ok += 1
                state.phase = "classifying"
                stats = await upsert_records(
                    result.records, spec.source_id,
                    prefs=prefs, taxonomy=taxonomy, horizon=horizon,
                )
                totals.merge(stats)
                await save_source_state(result, spec, record_count=result.raw_count)
            elif result.status == "not_modified":
                state.sources_not_modified += 1
                await save_source_state(result, spec)
            elif result.status == "error":
                state.sources_error += 1
                # A dead feed must be visible. A silently empty run looks
                # exactly like "nothing new today", which is the dangerous
                # failure mode here.
                state.errors.append(f"{spec.source_id}: {result.error}")
                await save_source_state(result, spec)

    state.listings_new = totals.created
    state.listings_updated = totals.updated
    state.listings_corroborated = totals.corroborated
    state.listings_deactivated = totals.deactivated


async def _phase_enrich(state: RunState, limit: int) -> None:
    """Fetch real job descriptions for listings that lack one."""
    state.phase = "enriching"
    # The limit bounds how many listings are ENRICHED, not how many rows are
    # examined. Applying it to the fetch and filtering afterwards meant a run
    # inspected 200 rows ordered by bucket_confidence and enriched only
    # whichever of those happened to need it — so listings below the cutoff
    # were reached slowly or never. Measured effect: 264 of 566 active
    # listings had no description at all.
    #
    # Enrichment is also no longer gated on draft_status: a description is the
    # product now, not an input to drafting, and a listing whose draft was
    # skipped still deserves one.
    pending = [
        row
        for row in await database.db.list_listings(limit=5000)
        if not row.get("description_text") and row.get("active", True)
    ][:limit]
    if not pending:
        return
    state.errors.append(f"enrichment: {len(pending)} listing(s) queued this run")

    cache = enrich_mod.BoardCache()
    async with enrich_mod.build_enrich_client() as client:
        for listing in pending:
            result = await enrich_mod.enrich_listing(
                listing, client=client, cache=cache
            )
            updates: dict[str, Any] = {}
            if result.ok:
                state.enriched_ok += 1
                state.enriched_by_tier[result.tier or "unknown"] = (
                    state.enriched_by_tier.get(result.tier or "unknown", 0) + 1
                )
                updates["description_text"] = result.description_text
                if result.deadline:
                    # A hand-typed deadline always wins over a scraped one.
                    if listing.get("deadline_source") != "manual":
                        updates["deadline"] = result.deadline
                        updates["deadline_source"] = result.deadline_source
            else:
                state.enriched_failed += 1
                # Without a real description a draft would be tailored against
                # a bare title, which is worse than no draft at all.
            if result.still_listed is False:
                updates["active"] = False
            if updates:
                await database.db.update_listing(listing["listing_id"], updates)


# A deadline this close outranks everything and is exempt from the backlog
# window: a listing that closes tomorrow cannot wait for tonight's quiet hours.
URGENT_DEADLINE_DAYS = 7

# Sorts after every real deadline, so undated listings never jump the queue.
_NO_DEADLINE = 10**6


def days_until_deadline(listing: dict[str, Any], *, now: datetime | None = None) -> int:
    """Whole days until this listing closes, or ``_NO_DEADLINE`` if unknown.

    Only ~2 of 547 active listings carry a deadline: no aggregator publishes
    one, and scanning descriptions for deadline language is overwhelmingly
    false positives ("deadline-driven environment"), so ``enrich.py``
    deliberately records a date only when it finds one next to a deadline
    word. This ordering therefore helps the handful of listings where the
    answer is actually known, and is a no-op for the rest — which is the
    honest extent of deadline prioritization available from this data.
    """
    raw = listing.get("deadline")
    if not raw:
        return _NO_DEADLINE
    try:
        due = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return _NO_DEADLINE
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (due - (now or datetime.now(timezone.utc))).days


def _is_urgent(listing: dict[str, Any]) -> bool:
    """A known deadline inside the urgent window (past deadlines included)."""
    return days_until_deadline(listing) <= URGENT_DEADLINE_DAYS


def _is_fresh(listing: dict[str, Any], *, hours: int = FRESH_LISTING_HOURS) -> bool:
    """True when we first saw this listing within the freshness window."""
    seen = listing.get("first_seen_at")
    if not seen:
        return False
    try:
        first_seen = datetime.fromisoformat(seen)
    except (TypeError, ValueError):
        return False
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - first_seen) <= timedelta(hours=hours)


async def _phase_report(state: RunState, *, notify: bool, digest: bool) -> None:
    """Tell a human what changed.

    Detection within 30 minutes is worth nothing if it never reaches anyone —
    "apply as early as possible" is a human action. Notifications cover the
    urgent case (a new, good-fit listing right now); the digest covers
    completeness. Entirely best-effort: a failure here must not fail a run that
    already did its real work.
    """
    if not (notify or digest):
        return
    state.phase = "reporting"
    try:
        from roleradar.notify import digest as notify_mod
        from roleradar.settings import settings

        rows = await database.db.list_listings(limit=1000)
        recent = notify_mod.listings_since(rows, hours=FRESH_LISTING_HOURS)

        if notify and state.listings_new:
            # Only listings first seen during THIS run are genuinely new; a
            # 24h window would re-announce the same roles every 30 minutes.
            newest = sorted(
                recent, key=lambda r: r.get("first_seen_at") or "", reverse=True
            )[: state.listings_new]
            state.notifications_sent = await notify_mod.notify_new_listings(newest)

        if digest:
            path = await notify_mod.write_digest(
                recent, data_dir=settings.data_dir, notify=False
            )
            if path:
                state.digest_path = str(path)
    except Exception as exc:  # noqa: BLE001 - reporting never fails a run
        logger.warning("Scout reporting failed: %s", exc)


async def _phase_advise(state: RunState) -> None:
    """Score every listing for fit and write per-listing application advice.

    This replaced automated drafting as the run's output. The reason is
    measured: an agent-written resume and a local-model one scored *identically*
    on the ATS composite (31.3), because the score is bounded by requirements
    the candidate genuinely lacks and by JD phrases nothing can match.
    Generating prose could not move that; telling him where he stands can.

    Costs nothing — pure Python, no LLM — so it recomputes for every listing on
    every run rather than caching a stale verdict against a master profile that
    changes.
    """
    state.phase = "advising"
    master: dict[str, Any] = {}
    if not master or not master.get("processed_data"):
        state.errors.append("no master resume — advice skipped")
        return
    profile = master["processed_data"]

    rows = [r for r in await database.db.list_listings(limit=5000) if r.get("active", True)]

    # One application per job, not per city: the same posting appears once per
    # location in the feeds (21 rows for a single RTX role), and the advice has
    # to say so or it reads as 21 separate opportunities.
    spread: dict[tuple[str, str], int] = {}
    for r in rows:
        spread[(r.get("company") or "", r.get("title") or "")] = (
            spread.get((r.get("company") or "", r.get("title") or ""), 0) + 1
        )

    # Requirement extraction and the user's skill profile are wired in Phase 5
    # (scout/requirements.py + prefs.skills). Until then advice runs with an
    # empty profile, which is a supported state: fit scoring drops out and the
    # logistics half - deadline countdown, duplicate-location spread, work-auth
    # caveat, "no deadline published, treat as rolling" - still runs. Advice
    # degrades, it never fails.
    keywords: dict[str, dict[str, Any]] = {}

    # Fit is ranked against the listings actually available, so the benchmarks
    # come from this corpus rather than a hardcoded bar.
    prelim = [
        advise_mod.advise(r, profile, keywords.get(r["listing_id"], {})).fit_ratio
        for r in rows
        if r["listing_id"] in keywords
    ]
    benchmarks = advise_mod.compute_benchmarks(prelim)

    written = 0
    for r in rows:
        kw = keywords.get(r["listing_id"], {})
        a = advise_mod.advise(
            r, profile, kw,
            duplicate_locations=spread.get((r.get("company") or "", r.get("title") or ""), 1),
            benchmarks=benchmarks,
        )
        await database.db.update_listing(r["listing_id"], {"advice": a.to_dict()})
        written += 1
    state.advised = written
    state.errors.append(
        f"advice: {written} listing(s); benchmarks median={benchmarks[0]:.2f} p75={benchmarks[1]:.2f}"
    )


# --- orchestration --------------------------------------------------------


async def run_scout(
    *,
    enrich: bool = True,
    enrich_limit: int = 200,
    notify: bool = True,
    digest: bool = True,
    state: RunState | None = None,
) -> RunState:
    """Execute one full run: fetch, store, enrich, advise, report."""
    state = state or RunState(run_id=str(uuid4()), started_at=_now())
    try:
        await _phase_fetch_and_store(state)
        if enrich:
            await _phase_enrich(state, limit=enrich_limit)
        await _phase_advise(state)
        await _phase_report(state, notify=notify, digest=digest)
        state.phase = "done"
    except asyncio.CancelledError:
        state.phase = "cancelled"
        state.cancelled = True
        raise
    except Exception as exc:  # noqa: BLE001 - a run reports failure, never crashes
        logger.error("Scout run failed: %s", exc, exc_info=True)
        state.phase = "failed"
        state.errors.append(str(exc)[:500])
    finally:
        state.finished_at = _now()
    return state


def _on_run_done(task: asyncio.Task) -> None:
    """Surface exceptions that would otherwise vanish into a bare task."""
    global _current_task
    _current_task = None
    if task.cancelled():
        logger.info("Scout run cancelled")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Scout run task raised: %s", exc, exc_info=exc)


async def start_scout_run(**kwargs: Any) -> RunState:
    """Kick a run in the background and return immediately.

    Raises ``ScoutAlreadyRunning`` if one is already in flight — a full run can
    take hours, so overlapping them would double-draft and thrash the model.
    """
    global _current_state, _current_task

    if _run_lock.locked():
        raise ScoutAlreadyRunning("a Scout run is already in progress")

    state = RunState(run_id=str(uuid4()), started_at=_now())
    _current_state = state

    async def _guarded() -> None:
        async with _run_lock:
            await run_scout(state=state, **kwargs)

    _current_task = asyncio.create_task(_guarded(), name=f"scout-run-{state.run_id}")
    _current_task.add_done_callback(_on_run_done)
    # Yield once so the task acquires the lock before the caller can re-check.
    await asyncio.sleep(0)
    return state


async def cancel_scout_run() -> None:
    """Cancel an in-flight run. Called during lifespan shutdown."""
    task = _current_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
        pass


