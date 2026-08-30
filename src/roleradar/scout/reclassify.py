"""Re-judge stored listings against the current taxonomy, without refetching.

This is the payoff for keeping `raw_record` and for labelling rather than
filtering at ingest. Add a specialty to taxonomy.yml, run this, and listings
that were already on disk pick up the new label - no network, no waiting for
the next scheduled run, and nothing lost because it was discarded months ago.
"""

from __future__ import annotations

import logging

from roleradar import db as database
from roleradar.prefs import load_prefs
from roleradar.scout.classify import classify
from roleradar.scout.normalize import ScoutRecord
from roleradar.taxonomy import load_taxonomy

logger = logging.getLogger(__name__)

__all__ = ["reclassify_all"]

_BATCH = 500


def _record_from_row(row: dict) -> ScoutRecord:
    """Rebuild the record a listing was created from.

    Prefers the stored columns over `raw_record`: the raw payload is per-feed
    and already normalized once, so re-normalizing it would duplicate work and
    risk drifting from what the row actually says.
    """
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
        raw=row.get("raw_record") or {},
    )


async def reclassify_all() -> dict[str, int]:
    """Reclassify every stored listing. Returns counts of what changed."""
    prefs = load_prefs()
    taxonomy = load_taxonomy()
    horizon = prefs.horizon()

    scanned = relabelled = failed = 0
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

    return {"scanned": scanned, "relabelled": relabelled, "failed": failed}
