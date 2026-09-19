"""Re-judge stored listings against the current taxonomy, without refetching.

This is the payoff for keeping `raw_record` and for labelling rather than
filtering at ingest. Add a specialty to taxonomy.yml, run this, and listings
that were already on disk pick up the new label - no network, no waiting for
the next scheduled run, and nothing lost because it was discarded months ago.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from roleradar import db as database
from roleradar.prefs import load_prefs
from roleradar.scout.classify import classify
from roleradar.scout.normalize import ScoutRecord
from roleradar.taxonomy import load_taxonomy

logger = logging.getLogger(__name__)

__all__ = ["reclassify_all"]

_BATCH = 500


def _spec_for(source_id: str):
    """The SourceSpec a stored row came from, including preference-built ones."""
    from roleradar.scout.normalize import normalize_himalayas, normalize_jobicy
    from roleradar.scout.sources import SOURCES_BY_ID

    if source_id in SOURCES_BY_ID:
        return SOURCES_BY_ID[source_id]
    prefix = source_id.split(":", 1)[0]
    normalizer = {"himalayas": normalize_himalayas, "jobicy": normalize_jobicy}.get(prefix)
    if normalizer is None:
        return None
    from roleradar.scout.sources.aggregators import SourceSpec

    return SourceSpec(source_id=source_id, url="", normalizer=normalizer)


def _record_from_row(row: dict) -> ScoutRecord:
    """Rebuild the record a listing was created from.

    Re-runs the source's own normalizer over the stored feed payload. Rebuilding
    from columns alone lost anything the normalizer derived but did not store as
    a column - most importantly `role_type`, which is how every plain-titled
    full-time role ("Data Engineer") was silently downgraded to "unknown" the
    first time anyone ran reclassify.

    Falls back to the columns when there is no payload or no normalizer.
    """
    spec = _spec_for(row["source_id"])
    raw = row.get("raw_record") or {}
    if spec is not None and raw:
        try:
            record = spec.normalizer(raw, row["source_id"])
        except Exception:  # noqa: BLE001 - fall back to columns below
            record = None
        if record is not None:
            if record.role_type is None and spec.role_type:
                record = replace(record, role_type=spec.role_type)
            return record

    return ScoutRecord(
        source_id=row["source_id"],
        external_id=row["external_id"],
        company=row["company"],
        title=row["title"],
        apply_url=row["apply_url"],
        company_url=row.get("company_url"),
        locations=row.get("locations") or [],
        is_remote=bool(row.get("is_remote")),
        terms=row.get("raw_terms") or [],
        category=row.get("raw_category"),
        sponsorship=row.get("sponsorship"),
        degrees=row.get("degrees") or [],
        date_posted=row.get("date_posted"),
        date_updated=row.get("date_updated"),
        active=bool(row.get("active", True)),
        role_type=spec.role_type if spec is not None else None,
        raw=raw,
    )


async def reclassify_all() -> dict[str, int]:
    """Reclassify every stored listing. Returns counts of what changed."""
    prefs = load_prefs()
    taxonomy = load_taxonomy()
    horizon = prefs.horizon()

    scanned = relabelled = role_changed = failed = 0
    offset = 0

    while True:
        rows = await database.db.list_listings(
            include_dismissed=True, include_inactive=True, limit=_BATCH, offset=offset
        )
        if not rows:
            break
        offset += len(rows)

        for row in rows:
            scanned += 1
            try:
                verdict = classify(
                    _record_from_row(row),
                    prefs=prefs,
                    taxonomy=taxonomy,
                    horizon=horizon,
                )
            except Exception as exc:  # noqa: BLE001 - one bad row is not fatal
                logger.warning("reclassify failed for %s: %s", row["listing_id"], exc)
                failed += 1
                continue

            before = await database.db.get_labels(row["listing_id"])
            after = {"domain": verdict.domains, "specialty": verdict.specialties}
            changed = {k: sorted(v) for k, v in before.items()} != {
                k: sorted(v) for k, v in after.items() if v
            }

            await database.db.update_listing(
                row["listing_id"],
                {
                    "term_id": verdict.term_id,
                    "role_type": verdict.role_type,
                    "classify_confidence": verdict.confidence,
                    "classify_reasons": verdict.reasons,
                    "work_auth_score": verdict.work_auth_score,
                    "needs_verification": verdict.needs_verification,
                    "taxonomy_version": taxonomy.version,
                },
            )
            await database.db.set_labels(row["listing_id"], after)
            if changed:
                relabelled += 1
            if verdict.role_type != row.get("role_type"):
                role_changed += 1

    return {
        "scanned": scanned,
        "relabelled": relabelled,
        "role_changed": role_changed,
        "failed": failed,
    }
