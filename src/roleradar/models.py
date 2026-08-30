"""SQLAlchemy ORM models for roleradar.

Two tables. `sources` tracks per-feed conditional-GET state so a fetch that has
not changed costs a 304 rather than a re-parse. `listings` holds every job the
scout has seen.

Timestamps are ISO-8601 strings rather than native datetimes: they are compared
lexically and returned to clients verbatim, and SQLite has no date type anyway.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    """Declarative base shared by every table."""


class Source(Base):
    """Conditional-GET bookkeeping for one opportunity feed.

    One row per feed. ``etag``/``last_modified`` are replayed as
    ``If-None-Match``/``If-Modified-Since`` so an unchanged upstream costs a
    304 instead of re-downloading the payload (the primary feed is ~10MB).
    """

    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String)
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String, nullable=True)
    last_checked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_success_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # ok | not_modified | error
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Listing(Base):
    """One scraped opportunity, classified into a bucket.

    Rows are never hard-deleted: a listing that disappears from a successful
    feed parse is marked ``active=False``, because tailored drafts and tracker
    cards reference it. ``raw_record`` keeps the normalized source payload so
    reclassification never requires a refetch — and so most future fields can
    be added without a schema migration.
    """

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_scout_listing_source_external"),
        Index("ix_listings_term_active", "term_id", "active"),
        Index("ix_listings_fingerprint", "fingerprint"),
    )

    listing_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String)
    # sha256 of normalized (company, title, canonical url) - cross-source dedupe.
    fingerprint: Mapped[str] = mapped_column(String)

    company: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    # Attribution link back to the aggregator. Always persisted, always rendered.
    company_url: Mapped[str | None] = mapped_column(String, nullable=True)
    apply_url: Mapped[str] = mapped_column(String)

    # greenhouse | ashby | lever | workday | other | None
    ats_vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    ats_tenant: Mapped[str | None] = mapped_column(String, nullable=True)
    ats_job_id: Mapped[str | None] = mapped_column(String, nullable=True)

    locations: Mapped[list] = mapped_column(JSON, default=list)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    # Verbatim feed terms - the audit trail behind the bucket decision.
    raw_terms: Mapped[list] = mapped_column(JSON, default=list)
    raw_category: Mapped[str | None] = mapped_column(String, nullable=True)
    sponsorship: Mapped[str | None] = mapped_column(String, nullable=True)
    degrees: Mapped[list] = mapped_column(JSON, default=list)
    date_posted: Mapped[str | None] = mapped_column(String, nullable=True)
    date_updated: Mapped[str | None] = mapped_column(String, nullable=True)

    # Computed from the calendar, e.g. "summer_2027". Null is legitimate: a
    # new-grad posting has no academic term.
    term_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # internship | new_grad | unknown
    role_type: Mapped[str] = mapped_column(String, default="unknown", index=True)
    classify_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Human-readable rule trace, surfaced in the UI so a decision is auditable.
    classify_reasons: Mapped[list] = mapped_column(JSON, default=list)
    # Which taxonomy version judged this row, so `reclassify` knows what is stale.
    taxonomy_version: Mapped[int] = mapped_column(Integer, default=0)
    # Null unless the user needs sponsorship; scoring is skipped otherwise.
    work_auth_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Part of the API contract, not just a UI concern - the classifier is a
    # heuristic and must never present itself as authoritative.
    needs_verification: Mapped[bool] = mapped_column(Boolean, default=False)

    deadline: Mapped[str | None] = mapped_column(String, nullable=True)
    # greenhouse | description | manual | unknown
    deadline_source: Mapped[str] = mapped_column(String, default="unknown")
    description_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-listing application advice: fit gaps, logistics, what to lead with,
    # and a priority score. Recomputed every run - it is cheap (no LLM) and
    # depends on the user's stated skills, which change.
    advice: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    # The feed record exactly as received. This is what makes reclassification
    # possible without refetching: when the taxonomy changes, every stored row
    # can be re-judged from its original payload.
    raw_record: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ListingLabel(Base):
    """One (kind, value) label on a listing - domain or specialty.

    A join table rather than a JSON column on `listings`: filtering by
    specialty is the main query the UI makes, and a JSON array cannot be
    indexed. Rows are replaced wholesale whenever a listing is reclassified.
    """

    __tablename__ = "listing_labels"
    __table_args__ = (
        UniqueConstraint("listing_id", "kind", "value", name="uq_listing_label"),
        Index("ix_listing_labels_kind_value", "kind", "value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(String, index=True)
    # "domain" | "specialty"
    kind: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
