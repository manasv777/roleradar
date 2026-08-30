"""Wire types for the API.

Deliberately absent: an enum of term ids. The original had one, and it meant a
client shipped with a fixed list of seasons that silently went stale. Terms are
plain strings here; labels come from `GET /terms`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TermResponse(BaseModel):
    id: str
    label: str
    season: str
    year: int


class SpecialtyResponse(BaseModel):
    id: str
    key: str
    label: str
    skills: list[str] = Field(default_factory=list)


class DomainResponse(BaseModel):
    id: str
    label: str
    specialties: list[SpecialtyResponse] = Field(default_factory=list)


class TaxonomyResponse(BaseModel):
    version: int
    domains: list[DomainResponse] = Field(default_factory=list)


class ListingResponse(BaseModel):
    listing_id: str
    source_id: str
    company: str
    title: str
    company_url: str | None = None
    apply_url: str
    locations: list[str] = Field(default_factory=list)
    is_remote: bool = False
    term_id: str | None = None
    role_type: str = "unknown"
    domains: list[str] = Field(default_factory=list)
    specialties: list[str] = Field(default_factory=list)
    classify_confidence: float = 0.0
    classify_reasons: list[str] = Field(default_factory=list)
    work_auth_score: float | None = None
    needs_verification: bool = False
    deadline: str | None = None
    deadline_source: str = "unknown"
    ats_vendor: str | None = None
    advice: dict[str, Any] | None = None
    active: bool = True
    dismissed: bool = False
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class ListingListResponse(BaseModel):
    listings: list[ListingResponse] = Field(default_factory=list)
    # Matches the CURRENT filters, so the client can page correctly. A total
    # that ignored the filters would offer pages that resolve to nothing.
    total: int = 0
    counts_by_term: dict[str, int] = Field(default_factory=dict)


class ListingDetailResponse(ListingResponse):
    description_text: str | None = None
    raw_terms: list[str] = Field(default_factory=list)
    raw_category: str | None = None
    sponsorship: str | None = None
    degrees: list[str] = Field(default_factory=list)


class ListingUpdate(BaseModel):
    """Only fields the user owns are writable."""

    dismissed: bool | None = None
    deadline: str | None = None


class SourceResponse(BaseModel):
    source_id: str
    url: str
    enabled: bool = True
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    record_count: int = 0
    note: str = ""


class RunRequest(BaseModel):
    enrich: bool = True
    enrich_limit: int = 200
    notify: bool = True
    digest: bool = True


class RunStateResponse(BaseModel):
    run_id: str
    started_at: str
    phase: str
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
    enriched_by_tier: dict[str, int] = Field(default_factory=dict)
    advised: int = 0
    notifications_sent: int = 0
    digest_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    cancelled: bool = False


class PreferencesResponse(BaseModel):
    version: int = 1
    interests: dict[str, list[str]] = Field(default_factory=dict)
    role_types: list[str] = Field(default_factory=list)
    terms: dict[str, Any] = Field(default_factory=dict)
    work_auth: dict[str, Any] = Field(default_factory=dict)
    locations: dict[str, Any] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    notifications: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
