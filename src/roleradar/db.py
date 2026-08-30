"""Async SQLite data layer.

The facade converts ORM rows to plain dicts so nothing above this module sees
SQLAlchemy objects. There is one engine: roleradar has no synchronous hot path
(the fork's sync engine existed only to read encrypted LLM keys, and roleradar
has no keys to read).

Single-writer assumption: exactly one process writes this file. The CLI's
`run --once` refuses to start when a server is reachable for that reason.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from roleradar.models import Base, Listing, ListingLabel, Source


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# A re-parse of an upstream feed may correct a listing's details, but it must
# never rewrite which listing the row *is*, nor resurrect state the user owns.
# `listing_id`, `source_id`, `external_id`, and `first_seen_at` are therefore
# absent from this set and cannot be patched through update_listing().
_SCOUT_LISTING_MUTABLE_FIELDS = frozenset(
    {
        "fingerprint",
        "company",
        "title",
        "company_url",
        "apply_url",
        "ats_vendor",
        "ats_tenant",
        "ats_job_id",
        "locations",
        "is_remote",
        "raw_terms",
        "raw_category",
        "sponsorship",
        "degrees",
        "date_posted",
        "date_updated",
        "term_id",
        "role_type",
        "classify_confidence",
        "classify_reasons",
        "taxonomy_version",
        "work_auth_score",
        "needs_verification",
        "deadline",
        "deadline_source",
        "description_text",
        "advice",
        "active",
        "dismissed",
        "last_seen_at",
        "raw_record",
    }
)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL keeps reads from blocking the writer, `busy_timeout` rides out brief
    contention, and `foreign_keys` is off by default in SQLite.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_async_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(path: Path) -> Engine:
    """Sync engine used only for table creation at startup."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def init_models_sync(engine: Engine) -> None:
    """Create all tables. Idempotent."""
    Base.metadata.create_all(engine)



def _apply_listing_filters(
    stmt,
    *,
    term_id: str | None,
    role_type: str | None,
    domain: str | None,
    specialty: str | None,
    min_confidence: float,
    include_dismissed: bool,
    include_inactive: bool,
):
    """The one place listing filters are expressed.

    Shared by `list_listings` and `count_listings` so the two can never drift -
    a count that disagrees with its list is what produces empty pages.
    """
    if term_id is not None:
        stmt = stmt.where(Listing.term_id == term_id)
    if role_type is not None:
        stmt = stmt.where(Listing.role_type == role_type)
    if domain is not None:
        stmt = stmt.where(
            Listing.listing_id.in_(
                select(ListingLabel.listing_id).where(
                    ListingLabel.kind == "domain", ListingLabel.value == domain
                )
            )
        )
    if specialty is not None:
        stmt = stmt.where(
            Listing.listing_id.in_(
                select(ListingLabel.listing_id).where(
                    ListingLabel.kind == "specialty", ListingLabel.value == specialty
                )
            )
        )
    if min_confidence > 0.0:
        stmt = stmt.where(Listing.classify_confidence >= min_confidence)
    if not include_dismissed:
        stmt = stmt.where(Listing.dismissed.is_(False))
    if not include_inactive:
        stmt = stmt.where(Listing.active.is_(True))
    return stmt


class Database:
    """Async facade over the two tables."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def initialize(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        sync_engine = make_sync_engine(path)
        try:
            init_models_sync(sync_engine)
        finally:
            sync_engine.dispose()
        self._path = path
        self._engine = make_async_engine(path)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database.initialize() was never called")
        return self._session_factory

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None

    @staticmethod
    def _source_to_dict(row: Source) -> dict[str, Any]:
        return {
            "source_id": row.source_id,
            "url": row.url,
            "etag": row.etag,
            "last_modified": row.last_modified,
            "last_checked_at": row.last_checked_at,
            "last_success_at": row.last_success_at,
            "last_status": row.last_status,
            "last_error": row.last_error,
            "record_count": row.record_count,
            "enabled": row.enabled,
        }

    @staticmethod
    def _listing_to_dict(row: Listing) -> dict[str, Any]:
        return {
            "listing_id": row.listing_id,
            "source_id": row.source_id,
            "external_id": row.external_id,
            "fingerprint": row.fingerprint,
            "company": row.company,
            "title": row.title,
            "company_url": row.company_url,
            "apply_url": row.apply_url,
            "ats_vendor": row.ats_vendor,
            "ats_tenant": row.ats_tenant,
            "ats_job_id": row.ats_job_id,
            "locations": row.locations or [],
            "is_remote": row.is_remote,
            "raw_terms": row.raw_terms or [],
            "raw_category": row.raw_category,
            "sponsorship": row.sponsorship,
            "degrees": row.degrees or [],
            "date_posted": row.date_posted,
            "date_updated": row.date_updated,
            "term_id": row.term_id,
            "role_type": row.role_type,
            "classify_confidence": row.classify_confidence,
            "classify_reasons": row.classify_reasons or [],
            "taxonomy_version": row.taxonomy_version,
            "work_auth_score": row.work_auth_score,
            "needs_verification": row.needs_verification,
            "deadline": row.deadline,
            "deadline_source": row.deadline_source,
            "description_text": row.description_text,
            "advice": row.advice,
            "active": row.active,
            "dismissed": row.dismissed,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "raw_record": row.raw_record or {},
        }

    # -- Resume operations --------------------------------------------------

    async def get_source(self, source_id: str) -> dict[str, Any] | None:
        """Get one feed's conditional-GET bookkeeping row."""
        async with self._session() as session:
            row = await session.get(Source, source_id)
            return self._source_to_dict(row) if row else None

    async def list_sources(self) -> list[dict[str, Any]]:
        """List every known feed, ordered by id."""
        async with self._session() as session:
            result = await session.execute(select(Source).order_by(Source.source_id))
            return [self._source_to_dict(row) for row in result.scalars().all()]

    async def upsert_source(
        self, source_id: str, url: str, updates: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create or update a feed row, applying ``updates`` to known columns."""
        async with self._session() as session:
            row = await session.get(Source, source_id)
            if row is None:
                row = Source(source_id=source_id, url=url)
                session.add(row)
            else:
                row.url = url
            for key in (
                "etag",
                "last_modified",
                "last_checked_at",
                "last_success_at",
                "last_status",
                "last_error",
                "record_count",
                "enabled",
            ):
                if updates and key in updates:
                    setattr(row, key, updates[key])
            await session.commit()
            await session.refresh(row)
            return self._source_to_dict(row)

    async def create_listing(self, listing: dict[str, Any]) -> dict[str, Any]:
        """Insert a listing. ``listing_id`` is generated when absent."""
        async with self._session() as session:
            payload = dict(listing)
            payload.setdefault("listing_id", str(uuid4()))
            now = _now()
            payload.setdefault("first_seen_at", now)
            payload.setdefault("last_seen_at", now)
            row = Listing(**payload)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._listing_to_dict(row)

    async def get_listing(self, listing_id: str) -> dict[str, Any] | None:
        """Get a listing by primary key."""
        async with self._session() as session:
            row = await session.get(Listing, listing_id)
            return self._listing_to_dict(row) if row else None

    async def get_listing_by_external(
        self, source_id: str, external_id: str
    ) -> dict[str, Any] | None:
        """Get a listing by its natural key — the upsert lookup."""
        async with self._session() as session:
            result = await session.execute(
                select(Listing).where(
                    Listing.source_id == source_id,
                    Listing.external_id == external_id,
                )
            )
            row = result.scalars().first()
            return self._listing_to_dict(row) if row else None

    async def get_listing_by_fingerprint(
        self, fingerprint: str
    ) -> dict[str, Any] | None:
        """Get the first listing matching a cross-source fingerprint."""
        async with self._session() as session:
            result = await session.execute(
                select(Listing)
                .where(Listing.fingerprint == fingerprint)
                .order_by(Listing.first_seen_at)
            )
            row = result.scalars().first()
            return self._listing_to_dict(row) if row else None

    async def update_listing(
        self, listing_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``updates`` to a listing's mutable columns.

        Only known columns are written; unknown keys are ignored rather than
        raising, so a caller passing a wider dict (e.g. a normalized record)
        does not need to pre-filter.
        """
        async with self._session() as session:
            row = await session.get(Listing, listing_id)
            if row is None:
                return None
            for key, value in updates.items():
                if key in _SCOUT_LISTING_MUTABLE_FIELDS:
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return self._listing_to_dict(row)

    async def list_listings(
        self,
        *,
        term_id: str | None = None,
        role_type: str | None = None,
        domain: str | None = None,
        specialty: str | None = None,
        min_confidence: float = 0.0,
        include_dismissed: bool = False,
        include_inactive: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query listings, most confident first."""
        async with self._session() as session:
            stmt = select(Listing)
            stmt = _apply_listing_filters(
                stmt,
                term_id=term_id,
                role_type=role_type,
                domain=domain,
                specialty=specialty,
                min_confidence=min_confidence,
                include_dismissed=include_dismissed,
                include_inactive=include_inactive,
            )
            stmt = (
                stmt.order_by(
                    Listing.classify_confidence.desc(),
                    Listing.date_posted.desc(),
                    Listing.first_seen_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return [self._listing_to_dict(row) for row in result.scalars().all()]

    async def count_listings(
        self,
        *,
        term_id: str | None = None,
        role_type: str | None = None,
        domain: str | None = None,
        specialty: str | None = None,
        min_confidence: float = 0.0,
        include_dismissed: bool = False,
        include_inactive: bool = False,
    ) -> int:
        """Count listings matching exactly the filters ``list_listings`` takes.

        The filter sets are kept identical deliberately. This count drives
        pagination, and a counter that ignored, say, `specialty` would report a
        total for the whole corpus and hand the UI page numbers that lead to
        empty pages.
        """
        async with self._session() as session:
            stmt = select(func.count()).select_from(Listing)
            stmt = _apply_listing_filters(
                stmt,
                term_id=term_id,
                role_type=role_type,
                domain=domain,
                specialty=specialty,
                min_confidence=min_confidence,
                include_dismissed=include_dismissed,
                include_inactive=include_inactive,
            )
            result = await session.execute(stmt)
            return int(result.scalar() or 0)

    async def set_labels(self, listing_id: str, labels: dict[str, list[str]]) -> None:
        """Replace a listing's labels wholesale.

        Replacement rather than merge: reclassification must be able to *remove*
        a label the taxonomy no longer assigns, or edits to taxonomy.yml could
        only ever add.
        """
        async with self._session() as session:
            async with session.begin():
                await session.execute(
                    delete(ListingLabel).where(ListingLabel.listing_id == listing_id)
                )
                session.add_all(
                    ListingLabel(listing_id=listing_id, kind=kind, value=value)
                    for kind, values in labels.items()
                    for value in dict.fromkeys(values)
                )

    async def get_labels(self, listing_id: str) -> dict[str, list[str]]:
        async with self._session() as session:
            result = await session.execute(
                select(ListingLabel).where(ListingLabel.listing_id == listing_id)
            )
            out: dict[str, list[str]] = {}
            for row in result.scalars().all():
                out.setdefault(row.kind, []).append(row.value)
            return out

    async def distinct_labels(self, kind: str) -> list[str]:
        """Every label value in use, for building filter menus."""
        async with self._session() as session:
            result = await session.execute(
                select(ListingLabel.value)
                .where(ListingLabel.kind == kind)
                .distinct()
                .order_by(ListingLabel.value)
            )
            return [v for (v,) in result.all()]

    async def mark_listings_inactive(
        self, source_id: str, seen_external_ids: set[str]
    ) -> int:
        """Deactivate listings from ``source_id`` absent from the latest parse.

        Rows are never deleted — tailored drafts and tracker cards reference
        them, so a vanished listing is marked inactive instead.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Listing).where(
                    Listing.source_id == source_id,
                    Listing.active.is_(True),
                )
            )
            changed = 0
            for row in result.scalars().all():
                if row.external_id not in seen_external_ids:
                    row.active = False
                    changed += 1
            if changed:
                await session.commit()
            return changed


db = Database()
