"""Persistence for Scout listings: upsert, change detection, deactivation.

Two invariants this module exists to protect:

1. **User and pipeline state is never clobbered by a re-parse.** A listing that
   already has a drafted resume, a tracker card, a hand-typed deadline, or a
   dismissal keeps them when the feed republishes the same row.

2. **Listings are never deleted.** A row that disappears upstream is marked
   ``active=False``, because tailored resumes and tracker cards reference it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from roleradar import db as database
from roleradar.prefs import Preferences
from roleradar.scout.classify import Verdict, classify
from roleradar.taxonomy import Taxonomy
from roleradar.terms import Term
from roleradar.scout.normalize import ScoutRecord, detect_ats, fingerprint
from roleradar.scout.sources import FetchResult, SourceSpec

logger = logging.getLogger(__name__)


@dataclass
class UpsertStats:
    """Row-level outcome counts for one source's parse."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    corroborated: int = 0
    skipped_out_of_scope: int = 0
    deactivated: int = 0
    # Rows that raised. Counted separately from skips so a broken feed cannot
    # masquerade as a quiet one.
    errors: int = 0

    def merge(self, other: "UpsertStats") -> None:
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.corroborated += other.corroborated
        self.skipped_out_of_scope += other.skipped_out_of_scope
        self.deactivated += other.deactivated
        self.errors += other.errors


async def load_source_state() -> dict[str, dict[str, Any]]:
    """Load every feed's stored ETag/Last-Modified, keyed by source id."""
    rows = await database.db.list_sources()
    return {row["source_id"]: row for row in rows}


async def save_source_state(
    result: FetchResult, spec: SourceSpec, *, record_count: int | None = None
) -> None:
    """Persist the outcome of one fetch.

    A ``not_modified`` result only bumps ``last_checked_at`` — the stored
    validators and record count stay as they were.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    updates: dict[str, Any] = {"last_checked_at": now, "last_status": result.status}

    if result.status == "ok":
        updates["last_success_at"] = now
        updates["last_error"] = None
        updates["etag"] = result.etag
        updates["last_modified"] = result.last_modified
        if record_count is not None:
            updates["record_count"] = record_count
    elif result.status == "not_modified":
        updates["last_success_at"] = now
        updates["last_error"] = None
    elif result.status == "error":
        # Truncated: the field is for a human glancing at the sources view.
        updates["last_error"] = (result.error or "unknown error")[:500]

    await database.db.upsert_source(spec.source_id, spec.url, updates)


def _derived_columns(record: ScoutRecord, verdict: Verdict) -> dict[str, Any]:
    """Columns computed from a record + its classification."""
    vendor, tenant, job_id = detect_ats(record.apply_url)
    return {
        "fingerprint": fingerprint(record.company, record.title, record.apply_url),
        "company": record.company,
        "title": record.title,
        "company_url": record.company_url,
        "apply_url": record.apply_url,
        "ats_vendor": vendor,
        "ats_tenant": tenant,
        "ats_job_id": job_id,
        "locations": record.locations,
        "is_remote": record.is_remote,
        "raw_terms": record.terms,
        "raw_category": record.category,
        "sponsorship": record.sponsorship,
        "degrees": record.degrees,
        "date_posted": record.date_posted,
        "date_updated": record.date_updated,
        "term_id": verdict.term_id,
        "role_type": verdict.role_type,
        "classify_confidence": verdict.confidence,
        "classify_reasons": verdict.reasons,
        "work_auth_score": verdict.work_auth_score,
        "needs_verification": verdict.needs_verification,
        "active": record.active,
        "raw_record": record.raw,
    }


async def _corroborate(
    existing: dict[str, Any], record: ScoutRecord, verdict: Verdict
) -> bool:
    """Fold a duplicate from another source into the existing row.

    Returns True when the existing row was actually improved. A second source
    listing the same role is evidence, not a new opportunity — so it raises
    confidence and can fill a classification the first source could not make
    (vanshb03 has no year, for instance), but never creates a second row.
    """
    updates: dict[str, Any] = {}
    reasons = list(existing.get("classify_reasons") or [])
    note = f"corroborated by {record.source_id}"

    if note not in reasons:
        reasons.append(note)
        updates["classify_reasons"] = reasons

    # A source that CAN place the term beats one that cannot. vanshb03 emits a
    # season with no year, so a feed that names the year fills a real gap.
    if verdict.term_id and not existing.get("term_id"):
        updates.update(
            term_id=verdict.term_id,
            classify_confidence=verdict.confidence,
            role_type=verdict.role_type,
            needs_verification=verdict.needs_verification,
            classify_reasons=reasons + list(verdict.reasons),
        )
    elif verdict.term_id and verdict.term_id == existing.get("term_id"):
        # Independent agreement on the same term is genuine evidence.
        current = float(existing.get("classify_confidence") or 0.0)
        boosted = min(1.0, current + 0.05)
        if boosted > current:
            updates["classify_confidence"] = boosted

    # Fill gaps the primary source left blank.
    for field in ("sponsorship", "company_url"):
        if not existing.get(field) and getattr(record, field, None):
            updates[field] = getattr(record, field)
    if record.is_remote and not existing.get("is_remote"):
        updates["is_remote"] = True

    if not updates:
        return False
    await database.db.update_listing(existing["listing_id"], updates)
    return True


def _worth_storing(verdict: Verdict, scope: str) -> bool:
    """Whether a labelled record is worth a row.

    `store_scope` is a knob rather than a constant because the tradeoff is real:

    * ``all``      - keep everything a feed publishes.
    * ``taxonomy`` - keep anything that looks like an early-career technical
      role, whether or not it matches the user's current interests. This is the
      default, and it is what makes widening your fields a reclassification
      rather than a re-fetch.
    * ``selected`` - keep only what matches the interests set today. Smallest
      database, but adding a field later cannot recover what was never stored.
    """
    if verdict.excluded:
        return False
    if scope == "all":
        return True
    if scope == "selected":
        return bool(verdict.domains)
    return bool(verdict.domains) or verdict.role_type != "unknown"


async def upsert_listing(
    record: ScoutRecord,
    *,
    prefs: Preferences,
    taxonomy: Taxonomy,
    horizon: list[Term],
) -> tuple[str | None, str]:
    """Classify and persist one record.

    Returns ``(listing_id, outcome)`` where outcome is one of ``created``,
    ``updated``, ``unchanged``, ``corroborated``, or ``skipped``.
    """
    verdict = classify(record, prefs=prefs, taxonomy=taxonomy, horizon=horizon)

    existing = await database.db.get_listing_by_external(record.source_id, record.external_id)

    if existing is None:
        if not _worth_storing(verdict, prefs.store_scope):
            # Not an early-career technical role at all. Note this is a much
            # narrower test than the original, which dropped anything outside
            # the user's current fields - that is precisely what made adding an
            # interest later require refetching the world.
            return (None, "skipped")

        if not record.active:
            # Already closed the first time we ever saw it. There is nothing to
            # apply to and no closure event to report, so it is noise — on the
            # first sync that would be several hundred dead rows. A listing we
            # HAVE seen open is different: when it later goes inactive we keep
            # it and mark it closed, which is the signal worth having.
            return (None, "skipped")

        # Same role already tracked from a different feed?
        fp = fingerprint(record.company, record.title, record.apply_url)
        twin = await database.db.get_listing_by_fingerprint(fp)
        if twin is not None and twin["source_id"] != record.source_id:
            changed = await _corroborate(twin, record, verdict)
            return (twin["listing_id"], "corroborated" if changed else "unchanged")

        columns = _derived_columns(record, verdict)
        columns.update(source_id=record.source_id, external_id=record.external_id)
        columns["taxonomy_version"] = taxonomy.version
        created = await database.db.create_listing(columns)
        await database.db.set_labels(
            created["listing_id"],
            {"domain": verdict.domains, "specialty": verdict.specialties},
        )
        return (created["listing_id"], "created")

    listing_id = existing["listing_id"]
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    # Cheap change detection: same upstream revision and same open/closed state
    # means nothing to re-derive.
    same_revision = (
        existing.get("date_updated") == record.date_updated
        and existing.get("active") == record.active
    )
    if same_revision:
        await database.db.update_listing(listing_id, {"last_seen_at": now})
        return (listing_id, "unchanged")

    updates = _derived_columns(record, verdict)
    updates["last_seen_at"] = now

    # Never let a re-parse undo the user's own decisions or the pipeline's work.
    if existing.get("deadline_source") == "manual":
        updates.pop("deadline", None)
    if existing.get("dismissed"):
        updates.pop("dismissed", None)

    # Deliberately absent: the original retired a listing that no longer matched
    # the user's fields by setting active=False. That conflated two different
    # things. `active` means "still listed upstream" and mark_listings_inactive
    # is its only owner; no longer matching a filter is a query concern, not a
    # lifecycle event.

    updates["taxonomy_version"] = taxonomy.version
    await database.db.update_listing(listing_id, updates)
    await database.db.set_labels(
        listing_id, {"domain": verdict.domains, "specialty": verdict.specialties}
    )
    return (listing_id, "updated")


async def upsert_records(
    records: list[ScoutRecord],
    source_id: str,
    *,
    prefs: Preferences,
    taxonomy: Taxonomy,
    horizon: list[Term],
) -> UpsertStats:
    """Persist a whole feed, isolating per-record failures."""
    stats = UpsertStats()
    seen: set[str] = set()

    for record in records:
        seen.add(record.external_id)
        try:
            _, outcome = await upsert_listing(
                record, prefs=prefs, taxonomy=taxonomy, horizon=horizon
            )
        except Exception as exc:  # noqa: BLE001 - one bad row cannot end a feed
            logger.warning("upsert failed for %s/%s: %s", source_id, record.external_id, exc)
            stats.errors += 1
            continue

        if outcome == "created":
            stats.created += 1
        elif outcome == "updated":
            stats.updated += 1
        elif outcome == "corroborated":
            stats.corroborated += 1
        elif outcome == "unchanged":
            stats.unchanged += 1
        elif outcome == "skipped":
            stats.skipped_out_of_scope += 1
        else:
            # An unrecognised outcome is a bug, not a silent skip. The original
            # folded this into the skip counter, which hid it.
            logger.warning("unknown upsert outcome %r for %s", outcome, record.external_id)
            stats.errors += 1

    stats.deactivated = await database.db.mark_listings_inactive(source_id, seen)
    return stats
