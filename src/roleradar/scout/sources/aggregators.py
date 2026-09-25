"""Feed definitions and conditional-GET fetching.

The primary feed is ~10 MB and republished every 30 minutes, so every request
replays the stored ``ETag``/``Last-Modified``. An unchanged upstream costs a
304 with no body instead of a full download.

Sources are fetched **sequentially** with a per-host delay. Four requests a day
needs no concurrency, and sequential is trivially rate-limited — these are
volunteer-run repos and public ATS APIs, so politeness matters more than speed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx

from roleradar.scout.normalize import (
    ScoutRecord,
    normalize_applyguy,
    normalize_simplify,
    normalize_vansh,
    normalize_zshah,
)

logger = logging.getLogger(__name__)

# Identifies us to feed operators so a misbehaving client can be traced back.
USER_AGENT = "roleradar/0.1 (personal job-search tool; +https://github.com/roleradar)"

# Polite pause between requests to distinct hosts.
INTER_REQUEST_DELAY_SECONDS = 1.0

# Transport retries. 4xx is never retried (it will not spontaneously succeed),
# and a 304 is a success, not a failure.
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 8.0)

FetchStatus = Literal["ok", "not_modified", "error", "disabled"]


@dataclass(frozen=True)
class SourceSpec:
    """One upstream feed."""

    source_id: str
    url: str
    normalizer: Callable[[dict[str, Any], str], ScoutRecord | None]
    # Some feeds wrap their array in an object, e.g. {"jobs": [...]}.
    root_key: str | None = None
    enabled_by_default: bool = True
    # Attribution / licensing note, surfaced in the sources view.
    note: str = ""
    # Offset pagination for search APIs that cap a page (Himalayas returns ~20
    # of 65). Without it a single fetch silently reports a fraction of the
    # results as if it were all of them.
    page_param: str | None = None
    # "offset" passes the number of records already read; "page" passes a
    # 1-based page number. Himalayas silently ignores `offset` and returns page
    # one for every value, so getting this wrong looks like success.
    page_style: str = "offset"
    max_pages: int = 1
    # Drop records that are not located in the US. Used by the worldwide remote
    # boards, where "data engineer" otherwise returns Argentina and Bulgaria.
    require_us: bool = False
    # What kind of list this is, when the feed itself does not say per record.
    # An internship list's "Associate Product Manager" is an internship posting;
    # without this, the title's "manager" read as a senior full-time role.
    role_type: str | None = None


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        source_id="simplify_intern",
        role_type="internship",
        url=(
            "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
            "dev/.github/scripts/listings.json"
        ),
        normalizer=normalize_simplify,
        note=(
            "SimplifyJobs — no license (all rights reserved). Personal use only; "
            "company_url attribution is preserved and the corpus is never republished."
        ),
    ),
    SourceSpec(
        source_id="vansh_intern",
        role_type="internship",
        url=(
            "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/"
            "dev/.github/scripts/listings.json"
        ),
        normalizer=normalize_vansh,
        note="vanshb03 — MIT. Bare season with no year; corroboration only.",
    ),
    SourceSpec(
        source_id="zshah_intern",
        role_type="internship",
        url=(
            "https://zshah101.github.io/"
            "Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/api/jobs.json"
        ),
        normalizer=normalize_zshah,
        root_key="jobs",
        note="zshah101 — MIT.",
    ),
    SourceSpec(
        source_id="applyguy_intern",
        role_type="internship",
        url="https://raw.githubusercontent.com/ApplyGuy/2027-Internships/main/data/internships.json",
        normalizer=normalize_applyguy,
        root_key="jobs",
        note="ApplyGuy — listingUrl carries the real ATS link.",
    ),
    SourceSpec(
        source_id="simplify_newgrad",
        role_type="new_grad",
        url=(
            "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
            "dev/.github/scripts/listings.json"
        ),
        normalizer=normalize_simplify,
        enabled_by_default=False,
        note="Full-time new-grad roles, not internships. Off by default.",
    ),
)

SOURCES_BY_ID: dict[str, SourceSpec] = {spec.source_id: spec for spec in SOURCES}


@dataclass
class FetchResult:
    """Outcome of one feed fetch."""

    source_id: str
    status: FetchStatus
    records: list[ScoutRecord] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    raw_count: int = 0


def build_client() -> httpx.AsyncClient:
    """Shared HTTP client for feed fetching.

    A generous read timeout: the primary feed is ~10 MB and GitHub's raw host
    can be slow under load.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=120.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def _is_us_record(record: ScoutRecord) -> bool:
    """True when at least one location is recognisably in the US.

    Deliberately strict: "Anywhere" and "Remote" do not count. A worldwide
    board's idea of remote includes roles that cannot be taken from the US.
    """
    from roleradar.scout.classify import _location_is_us

    return any(_location_is_us(loc) is True for loc in record.locations)


def _parse_payload(text: str, spec: SourceSpec) -> list[dict[str, Any]]:
    """Parse a feed body into a list of raw records."""
    data = json.loads(text)
    if spec.root_key is not None:
        if not isinstance(data, dict):
            raise ValueError(f"expected an object with key '{spec.root_key}'")
        data = data.get(spec.root_key, [])
    if not isinstance(data, list):
        raise ValueError("feed payload is not a list of records")
    return [item for item in data if isinstance(item, dict)]


async def fetch_source(
    spec: SourceSpec,
    *,
    client: httpx.AsyncClient,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Fetch and normalize one feed, honoring conditional-GET headers.

    Never raises: every failure is reported as ``status="error"`` so one dead
    source cannot abort a run.
    """
    if spec.page_param and spec.max_pages > 1:
        return await _fetch_paginated(spec, client=client)
    return await _fetch_one(spec, client=client, etag=etag, last_modified=last_modified)


async def _fetch_paginated(spec: SourceSpec, *, client: httpx.AsyncClient) -> FetchResult:
    """Walk offset pages until one comes back empty or adds nothing new.

    Each page goes through `_fetch_one`, so retries and error handling are the
    same as for any other feed. A failure on page one is a failed source; a
    failure after that keeps what was already read rather than discarding it.
    """
    records: list[ScoutRecord] = []
    seen: set[str] = set()
    raw_total = 0
    joiner = "&" if "?" in spec.url else "?"

    for page in range(spec.max_pages):
        value = page + 1 if spec.page_style == "page" else len(seen)
        page_spec = replace(
            spec,
            url=f"{spec.url}{joiner}{spec.page_param}={value}" if page else spec.url,
            page_param=None,
            max_pages=1,
        )
        result = await _fetch_one(page_spec, client=client)
        if result.status != "ok":
            if page == 0:
                return result
            logger.warning("%s: page %d failed, keeping %d records", spec.source_id, page, len(records))
            break
        raw_total += result.raw_count
        fresh = [r for r in result.records if r.external_id not in seen]
        if not fresh:
            break
        for record in fresh:
            seen.add(record.external_id)
            records.append(record)
        await asyncio.sleep(INTER_REQUEST_DELAY_SECONDS)

    return FetchResult(
        source_id=spec.source_id, status="ok", records=records, raw_count=raw_total
    )


async def _fetch_one(
    spec: SourceSpec,
    *,
    client: httpx.AsyncClient,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_error: str | None = None
    for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
        try:
            response = await client.get(spec.url, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
            logger.warning("Scout fetch %s failed (attempt %d): %s", spec.source_id, attempt + 1, exc)
        else:
            if response.status_code == 304:
                # The happy path: upstream unchanged, no body transferred.
                return FetchResult(
                    source_id=spec.source_id,
                    status="not_modified",
                    etag=etag,
                    last_modified=last_modified,
                )
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "Scout fetch %s got %s (attempt %d)",
                    spec.source_id,
                    response.status_code,
                    attempt + 1,
                )
            elif response.status_code >= 400:
                # A 4xx will not spontaneously start working; fail immediately.
                # A 404 here usually means the feed repo was renamed — that must
                # surface as a visible error, never as an empty successful run.
                return FetchResult(
                    source_id=spec.source_id,
                    status="error",
                    error=f"HTTP {response.status_code} — feed may have moved or been renamed",
                )
            else:
                try:
                    # ~10MB of JSON blocks the event loop for a noticeable
                    # moment; parse it off the loop.
                    raw_records = await asyncio.to_thread(_parse_payload, response.text, spec)
                except (json.JSONDecodeError, ValueError) as exc:
                    return FetchResult(
                        source_id=spec.source_id,
                        status="error",
                        error=f"malformed payload: {exc}",
                    )
                records = [
                    record
                    for item in raw_records
                    if (record := spec.normalizer(item, spec.source_id)) is not None
                ]
                if spec.require_us:
                    records = [r for r in records if _is_us_record(r)]
                if spec.role_type:
                    # ScoutRecord is frozen; stamp by copying.
                    records = [
                        r if r.role_type else replace(r, role_type=spec.role_type)
                        for r in records
                    ]
                return FetchResult(
                    source_id=spec.source_id,
                    status="ok",
                    records=records,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    raw_count=len(raw_records),
                )

        if attempt < len(_RETRY_BACKOFF_SECONDS):
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])

    return FetchResult(source_id=spec.source_id, status="error", error=last_error)


async def fetch_all(
    specs: Sequence[SourceSpec],
    *,
    source_state: dict[str, dict[str, Any]],
    client: httpx.AsyncClient | None = None,
) -> list[FetchResult]:
    """Fetch every enabled feed sequentially, replaying stored validators."""
    owns_client = client is None
    active_client = client or build_client()
    results: list[FetchResult] = []
    try:
        for index, spec in enumerate(specs):
            state = source_state.get(spec.source_id) or {}
            if state.get("enabled") is False or (
                not state and not spec.enabled_by_default
            ):
                results.append(FetchResult(source_id=spec.source_id, status="disabled"))
                continue
            if index > 0:
                await asyncio.sleep(INTER_REQUEST_DELAY_SECONDS)
            results.append(
                await fetch_source(
                    spec,
                    client=active_client,
                    etag=state.get("etag"),
                    last_modified=state.get("last_modified"),
                )
            )
    finally:
        if owns_client:
            await active_client.aclose()
    return results
