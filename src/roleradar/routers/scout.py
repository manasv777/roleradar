"""Scout HTTP API."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from roleradar import db as database
from roleradar.prefs import Preferences, load_prefs, save_prefs
from roleradar.schemas.scout import (
    DomainResponse,
    ListingDetailResponse,
    ListingListResponse,
    ListingResponse,
    ListingUpdate,
    PreferencesResponse,
    RunRequest,
    RunStateResponse,
    SourceResponse,
    SpecialtyResponse,
    TaxonomyResponse,
    TermResponse,
)
from roleradar.scout import runner
from roleradar.scout.sources import SOURCES_BY_ID
from roleradar.taxonomy import load_taxonomy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["scout"])

db = database.db


async def _with_labels(row: dict[str, Any]) -> dict[str, Any]:
    labels = await db.get_labels(row["listing_id"])
    return {
        **row,
        "domains": labels.get("domain", []),
        "specialties": labels.get("specialty", []),
    }


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/terms", response_model=list[TermResponse])
async def list_terms() -> list[TermResponse]:
    """The terms currently in range. Computed, never a stored list."""
    return [
        TermResponse(id=t.id, label=t.label, season=t.season, year=t.year)
        for t in load_prefs().horizon(date.today())
    ]


@router.get("/taxonomy", response_model=TaxonomyResponse)
async def get_taxonomy() -> TaxonomyResponse:
    taxonomy = load_taxonomy()
    return TaxonomyResponse(
        version=taxonomy.version,
        domains=[
            DomainResponse(
                id=d.id,
                label=d.label,
                specialties=[
                    SpecialtyResponse(
                        id=s.id, key=s.key, label=s.label, skills=list(s.skills)
                    )
                    for s in d.specialties
                ],
            )
            for d in taxonomy.domains
        ],
    )


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences() -> PreferencesResponse:
    return PreferencesResponse(**load_prefs().to_dict())


@router.put("/preferences", response_model=PreferencesResponse)
async def put_preferences(payload: dict[str, Any]) -> PreferencesResponse:
    prefs = Preferences.from_dict(payload)
    save_prefs(prefs)
    return PreferencesResponse(**prefs.to_dict())


@router.get("/listings", response_model=ListingListResponse)
async def list_listings(
    term_id: str | None = Query(None),
    role_type: str | None = Query(None),
    domain: str | None = Query(None),
    specialty: str | None = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    include_dismissed: bool = Query(False),
    include_inactive: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ListingListResponse:
    """Listings, most confident first, paginated."""
    filters = dict(
        term_id=term_id,
        role_type=role_type,
        domain=domain,
        specialty=specialty,
        min_confidence=min_confidence,
        include_dismissed=include_dismissed,
        include_inactive=include_inactive,
    )
    try:
        rows = await db.list_listings(**filters, limit=limit, offset=offset)
        # Same filters as the query above, deliberately: a total computed over
        # anything else produces page numbers that lead nowhere.
        total = await db.count_listings(**filters)
        counts: dict[str, int] = {}
        for term in load_prefs().horizon(date.today()):
            counts[term.id] = await db.count_listings(term_id=term.id)
    except Exception as exc:  # noqa: BLE001
        logger.error("listing query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not load listings.")

    return ListingListResponse(
        listings=[ListingResponse(**await _with_labels(r)) for r in rows],
        total=total,
        counts_by_term=counts,
    )


@router.get("/listings/{listing_id}", response_model=ListingDetailResponse)
async def get_listing(listing_id: str) -> ListingDetailResponse:
    row = await db.get_listing(listing_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such listing.")
    return ListingDetailResponse(**await _with_labels(row))


@router.patch("/listings/{listing_id}", response_model=ListingResponse)
async def update_listing(listing_id: str, payload: ListingUpdate) -> ListingResponse:
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    if "deadline" in updates:
        # A hand-typed deadline outranks anything a later parse infers.
        updates["deadline_source"] = "manual"
    row = await db.update_listing(listing_id, updates)
    if row is None:
        raise HTTPException(status_code=404, detail="No such listing.")
    return ListingResponse(**await _with_labels(row))


@router.get("/sources", response_model=list[SourceResponse])
async def list_sources() -> list[SourceResponse]:
    """Every configured source, including ones that have never been fetched.

    Showing only rows that exist would hide a feed that has silently failed
    since day one, which is the case worth surfacing most.
    """
    stored = {row["source_id"]: row for row in await db.list_sources()}
    out: list[SourceResponse] = []
    for source_id, spec in SOURCES_BY_ID.items():
        row = stored.get(source_id, {})
        out.append(
            SourceResponse(
                source_id=source_id,
                url=spec.url,
                enabled=bool(row.get("enabled", spec.enabled_by_default)),
                last_checked_at=row.get("last_checked_at"),
                last_success_at=row.get("last_success_at"),
                last_status=row.get("last_status"),
                last_error=row.get("last_error"),
                record_count=int(row.get("record_count") or 0),
                note=spec.note,
            )
        )
    return out


@router.post("/runs", response_model=RunStateResponse, status_code=202)
async def start_run(payload: RunRequest | None = None) -> RunStateResponse:
    payload = payload or RunRequest()
    try:
        state = await runner.start_scout_run(**payload.model_dump())
    except runner.ScoutAlreadyRunning:
        raise HTTPException(status_code=409, detail="A run is already in flight.")
    return RunStateResponse(**state.to_dict())


@router.get("/runs/current", response_model=RunStateResponse | None)
async def current_run() -> RunStateResponse | None:
    state = runner.get_run_state()
    return RunStateResponse(**state.to_dict()) if state else None


@router.delete("/runs/current")
async def cancel_run() -> dict[str, bool]:
    await runner.cancel_scout_run()
    return {"cancelled": True}


@router.post("/reclassify")
async def reclassify() -> dict[str, int]:
    """Re-judge every stored listing against the current taxonomy.

    This is what makes editing taxonomy.yml cheap: because each row keeps the
    feed record it came from, nothing has to be fetched again.
    """
    from roleradar.scout.reclassify import reclassify_all

    return await reclassify_all()
