"""Fetch the real job description (and any deadline) from the ATS board.

The aggregate feeds carry title, company, and location — nothing more. Tailoring
a resume against a bare title produces garbage, so this step is what makes a
draft worth generating at all. A listing whose enrichment fails is marked
``skipped`` rather than drafted badly.

On deadlines, the honest position: they are almost never published. Greenhouse
exposes an ``application_deadline`` field that is null across entire boards, and
scanning description bodies for deadline language is overwhelmingly false
positives ("deadline-driven environment"). So a date is recorded only when a
source genuinely supplies one, and the UI shows "—" otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Board fetches are per-tenant, so one request covers every listing at that
# company. Concurrency is deliberately small — these are other people's APIs.
_MAX_CONCURRENT_HOSTS = 3
_PER_HOST_DELAY_SECONDS = 1.0

GREENHOUSE_BOARD = "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true"
ASHBY_BOARD = "https://api.ashbyhq.com/posting-api/job-board/{tenant}"
LEVER_BOARD = "https://api.lever.co/v0/postings/{tenant}?mode=json"
SMARTRECRUITERS_BOARD = (
    "https://api.smartrecruiters.com/v1/companies/{tenant}/postings?limit=100"
)
SMARTRECRUITERS_POSTING = (
    "https://api.smartrecruiters.com/v1/companies/{tenant}/postings/{job_id}"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")

# A real application deadline: a deadline word followed closely by an actual
# date. Prose like "deadline-driven environment" has no date and never matches.
_DEADLINE_RE = re.compile(
    r"\b(?:applications?\s+(?:close|are\s+due|must\s+be\s+received)|apply\s+by|"
    r"application\s+deadline|deadline\s+to\s+apply|last\s+day\s+to\s+apply)\b"
    r"[^.\n]{0,60}?"
    r"(\d{4}-\d{2}-\d{2}|"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


@dataclass
class Enrichment:
    """Result of enriching one listing."""

    description_text: str | None = None
    deadline: str | None = None
    deadline_source: str = "unknown"
    # False when the board no longer lists this job — strong evidence it closed.
    still_listed: bool | None = None
    # Which cascade tier produced the description: ats_json | workday | rendered.
    tier: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.description_text)


@dataclass
class BoardCache:
    """Per-run cache of fetched ATS boards, keyed by (vendor, tenant)."""

    boards: dict[tuple[str, str], dict[str, dict[str, Any]] | None] = field(
        default_factory=dict
    )

    def has(self, vendor: str, tenant: str) -> bool:
        return (vendor, tenant) in self.boards


def html_to_text(html: str) -> str:
    """Strip tags and collapse whitespace. No new dependency for one job."""
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def extract_deadline_from_text(text: str) -> str | None:
    """Find a genuine application deadline in a description body.

    Requires a deadline phrase AND a nearby date. Returns ISO ``YYYY-MM-DD``.
    """
    if not text:
        return None
    match = _DEADLINE_RE.search(text)
    if not match:
        return None
    raw = match.group(1).strip().rstrip(",")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    parts = re.match(
        r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", raw
    )
    if not parts:
        return None
    month = _MONTHS.get(parts.group(1).lower())
    if not month:
        return None
    return f"{int(parts.group(3)):04d}-{month:02d}-{int(parts.group(2)):02d}"


async def _fetch_greenhouse(
    tenant: str, client: httpx.AsyncClient
) -> dict[str, dict[str, Any]] | None:
    """Fetch a Greenhouse board, keyed by job id (as a string)."""
    response = await client.get(GREENHOUSE_BOARD.format(tenant=tenant))
    if response.status_code != 200:
        return None
    jobs = response.json().get("jobs", [])
    return {str(job.get("id")): job for job in jobs if job.get("id") is not None}


async def _fetch_ashby(
    tenant: str, client: httpx.AsyncClient
) -> dict[str, dict[str, Any]] | None:
    response = await client.get(ASHBY_BOARD.format(tenant=tenant))
    if response.status_code != 200:
        return None
    jobs = response.json().get("jobs", [])
    return {str(job.get("id")): job for job in jobs if job.get("id") is not None}


async def _fetch_lever(
    tenant: str, client: httpx.AsyncClient
) -> dict[str, dict[str, Any]] | None:
    response = await client.get(LEVER_BOARD.format(tenant=tenant))
    if response.status_code != 200:
        return None
    jobs = response.json()
    if not isinstance(jobs, list):
        return None
    return {str(job.get("id")): job for job in jobs if job.get("id") is not None}


async def _fetch_smartrecruiters(
    tenant: str, client: httpx.AsyncClient
) -> dict[str, dict[str, Any]] | None:
    """SmartRecruiters lists postings board-level, but bodies are per-posting.

    The list call is cheap and tells us the posting exists; the body is fetched
    lazily in ``_extract`` only for the listing we actually want.
    """
    response = await client.get(SMARTRECRUITERS_BOARD.format(tenant=tenant))
    if response.status_code != 200:
        return None
    content = response.json().get("content", [])
    return {str(job.get("id")): job for job in content if job.get("id") is not None}


_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "ashby": _fetch_ashby,
    "lever": _fetch_lever,
    "smartrecruiters": _fetch_smartrecruiters,
}


def _extract(vendor: str, job: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Pull (description, deadline, deadline_source) out of one board entry."""
    if vendor == "greenhouse":
        description = html_to_text(job.get("content") or "")
        deadline = job.get("application_deadline")
        if deadline:
            # Greenhouse gives a full timestamp; the date is what matters.
            return (description, str(deadline)[:10], "greenhouse")
        return (description, None, "unknown")

    if vendor in ("ashby", "lever"):
        description = job.get("descriptionPlain") or html_to_text(
            job.get("descriptionHtml") or job.get("description") or ""
        )
        return (description, None, "unknown")

    if vendor == "smartrecruiters":
        # The list response carries no body; jobAd is fetched per posting.
        ad = job.get("jobAd") or {}
        sections = (ad.get("sections") or {}) if isinstance(ad, dict) else {}
        parts = [
            (sections.get(name) or {}).get("text", "")
            for name in ("companyDescription", "jobDescription", "qualifications")
        ]
        return (html_to_text("\n".join(p for p in parts if p)), None, "unknown")

    return (None, None, "unknown")


async def _tier_ats_json(
    listing: dict[str, Any], client: httpx.AsyncClient, cache: BoardCache
) -> Enrichment | None:
    """Tier 1 — the documented public JSON APIs. Fast, free, reliable."""
    vendor = listing.get("ats_vendor")
    tenant = listing.get("ats_tenant")
    job_id = listing.get("ats_job_id")
    if vendor not in _FETCHERS or not tenant:
        return None

    key = (vendor, tenant)
    if key not in cache.boards:
        try:
            cache.boards[key] = await _FETCHERS[vendor](tenant, client)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Scout enrich failed for %s/%s: %s", vendor, tenant, exc)
            cache.boards[key] = None
        await asyncio.sleep(_PER_HOST_DELAY_SECONDS)

    board = cache.boards.get(key)
    if board is None:
        return None

    job = board.get(str(job_id)) if job_id else None
    if job is None:
        # The board loaded but this requisition is gone — better evidence that
        # it closed than the aggregate feed's own `active` flag.
        return Enrichment(
            still_listed=False,
            error=f"job {job_id} no longer on the {vendor} board",
        )

    if vendor == "smartrecruiters":
        try:
            detail = await client.get(
                SMARTRECRUITERS_POSTING.format(tenant=tenant, job_id=job_id)
            )
            if detail.status_code == 200:
                job = detail.json()
        except (httpx.HTTPError, ValueError):
            pass

    description, deadline, source = _extract(vendor, job)
    if not description:
        return None

    if deadline is None:
        recovered = extract_deadline_from_text(description)
        if recovered:
            deadline, source = recovered, "description"

    return Enrichment(
        description_text=description,
        deadline=deadline,
        deadline_source=source,
        still_listed=True,
        tier="ats_json",
    )


def _workday_cxs_url(apply_url: str) -> str | None:
    """Rewrite a Workday careers URL into its CXS job-detail endpoint.

    ``{tenant}.wd{N}.myworkdayjobs.com/[locale/]{site}/job/{path}`` becomes
    ``.../wday/cxs/{tenant}/{site}/job/{path}``. Undocumented and known to be
    inconsistent between tenants, which is why this is a fallback tier and its
    failures are not treated as errors.
    """
    try:
        parts = httpx.URL(apply_url)
    except Exception:  # noqa: BLE001
        return None
    host = parts.host
    if "myworkdayjobs.com" not in host:
        return None
    tenant = host.split(".", 1)[0]
    segments = [s for s in parts.path.split("/") if s]
    if "job" not in segments:
        return None
    job_index = segments.index("job")
    if job_index == 0:
        return None
    site = segments[job_index - 1]
    rest = "/".join(segments[job_index:])
    return f"https://{host}/wday/cxs/{tenant}/{site}/{rest}"


async def _tier_workday(
    listing: dict[str, Any], client: httpx.AsyncClient
) -> Enrichment | None:
    """Tier 2 — Workday's undocumented CXS endpoint. Best effort only."""
    if listing.get("ats_vendor") != "workday":
        return None
    url = _workday_cxs_url(listing.get("apply_url") or "")
    if not url:
        return None
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    await asyncio.sleep(_PER_HOST_DELAY_SECONDS)
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    info = payload.get("jobPostingInfo") or {}
    description = html_to_text(info.get("jobDescription") or "")
    if not description:
        return None
    return Enrichment(
        description_text=description,
        deadline=extract_deadline_from_text(description),
        deadline_source="description" if extract_deadline_from_text(description) else "unknown",
        still_listed=True,
        tier="workday",
    )


# Chrome that a human would use — some career sites serve a stub to unknown UAs.
_RENDER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_RENDER_TIMEOUT_MS = 25_000
_MIN_RENDERED_CHARS = 600


async def _tier_render(listing: dict[str, Any]) -> Enrichment | None:
    """Tier 3 — render the page and read the text.

    The universal fallback: it works regardless of ATS, including the direct
    company career sites that are the single largest group. Costs a page load
    each, so it only runs after the JSON tiers decline.
    """
    from roleradar.scout.browser import get_shared_browser

    url = listing.get("apply_url")
    if not url:
        return None
    browser = await get_shared_browser()
    if browser is None:
        return None

    page = None
    try:
        page = await browser.new_page(user_agent=_RENDER_UA)
        await page.goto(url, wait_until="domcontentloaded", timeout=_RENDER_TIMEOUT_MS)
        # Career pages hydrate their body after first paint.
        await page.wait_for_timeout(2000)
        text = await page.inner_text("body")
    except Exception as exc:  # noqa: BLE001 - rendering is best effort
        logger.info("Scout render failed for %s: %s", url, exc)
        return None
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    text = _BLANK_RE.sub("\n\n", (text or "").strip())
    if len(text) < _MIN_RENDERED_CHARS:
        # Too short to be a real posting — usually a login wall or a JS stub.
        return None

    deadline = extract_deadline_from_text(text)
    return Enrichment(
        description_text=text,
        deadline=deadline,
        deadline_source="description" if deadline else "unknown",
        still_listed=True,
        tier="rendered",
    )


async def enrich_listing(
    listing: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    cache: BoardCache,
    allow_render: bool = True,
) -> Enrichment:
    """Enrich one listing, cheapest tier first. Never raises.

    The cascade exists because the GitHub feeds are a discovery layer only —
    none of them carry job descriptions — and no single ATS covers more than a
    quarter of the listings they surface.
    """
    result = await _tier_ats_json(listing, client, cache)
    if result is not None and result.ok:
        return result
    # A confirmed delisting from tier 1 is a real answer; don't try to render it.
    if result is not None and result.still_listed is False:
        return result

    workday = await _tier_workday(listing, client)
    if workday is not None and workday.ok:
        return workday

    if allow_render:
        rendered = await _tier_render(listing)
        if rendered is not None and rendered.ok:
            return rendered

    vendor = listing.get("ats_vendor")
    return Enrichment(error=f"no description obtainable (vendor={vendor!r})")


def build_enrich_client() -> httpx.AsyncClient:
    """HTTP client for ATS board fetches."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, read=60.0),
        follow_redirects=True,
        headers={
            "User-Agent": "resume-matcher-scout/1.0 (personal job-search tool)",
            "Accept": "application/json",
        },
    )


def enrich_semaphore() -> asyncio.Semaphore:
    """Bound concurrent board fetches."""
    return asyncio.Semaphore(_MAX_CONCURRENT_HOSTS)
