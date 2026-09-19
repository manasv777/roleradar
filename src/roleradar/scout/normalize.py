"""Feed records -> a single normalized ``ScoutRecord`` shape.

Every upstream feed has its own schema. This module is the only place that
knows about those differences; everything downstream (classify, store, enrich)
operates on ``ScoutRecord`` alone.

Normalizers are deliberately total: a malformed or unusable record returns
``None`` rather than raising, so one bad row in a 14k-record feed can never
abort a run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class ScoutRecord:
    """One opportunity, normalized across sources."""

    source_id: str
    external_id: str
    company: str
    title: str
    apply_url: str
    company_url: str | None = None
    locations: list[str] = field(default_factory=list)
    # Verbatim season/term strings, e.g. ["Spring 2027"]. Empty when the feed
    # carries no usable term signal at all.
    terms: list[str] = field(default_factory=list)
    category: str | None = None
    sponsorship: str | None = None
    degrees: list[str] = field(default_factory=list)
    is_remote: bool = False
    active: bool = True
    date_posted: str | None = None
    date_updated: str | None = None
    # internship | new_grad | full_time, when the feed itself says so. The
    # classifier reads this before guessing from the title; it existed as a
    # getattr() probe with no field behind it, so no feed could ever set it.
    role_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# --- primitives -----------------------------------------------------------

# Tracking params that change per-visit and would otherwise defeat dedupe.
_TRACKING_PREFIXES = ("utm_", "gh_", "lever-", "ref_")
_TRACKING_KEYS = frozenset({"ref", "source", "src", "gh_jid", "gh_src", "trackingid"})

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bwork from home\b", re.IGNORECASE)


def epoch_to_iso(value: Any) -> str | None:
    """Convert a Unix epoch (seconds) to an ISO-8601 UTC string.

    Accepts ints, floats, and numeric strings. Returns ``None`` for anything
    unparseable or non-positive — feeds do use ``0`` as a null sentinel.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    # Some feeds emit milliseconds; anything past year ~5138 is a ms timestamp.
    if seconds > 1e11:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def canonical_url(url: str) -> str:
    """Lowercase the host and strip tracking params and fragments.

    Used only for fingerprinting — the original ``apply_url`` is always kept
    verbatim for the user to click.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    kept: list[str] = []
    if parts.query:
        for pair in parts.query.split("&"):
            if not pair:
                continue
            key = pair.split("=", 1)[0].lower()
            if key in _TRACKING_KEYS or key.startswith(_TRACKING_PREFIXES):
                continue
            kept.append(pair)

    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, "&".join(kept), "")
    )


def _normalize_text(value: str) -> str:
    """Collapse to lowercase alphanumeric words for stable comparison."""
    return _NON_ALNUM_RE.sub(" ", _WS_RE.sub(" ", (value or "").lower())).strip()


def fingerprint(company: str, title: str, apply_url: str) -> str:
    """Stable cross-source identity for one opportunity.

    Two feeds listing the same role produce the same fingerprint, which is how
    a supplementary source corroborates the primary one instead of creating a
    duplicate row.
    """
    basis = "|".join(
        (_normalize_text(company), _normalize_text(title), canonical_url(apply_url))
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def detect_ats(url: str) -> tuple[str | None, str | None, str | None]:
    """Parse an apply URL into ``(vendor, tenant, job_id)``.

    The tenant is what the enrichment step needs: ATS board APIs are
    board-level, so one fetch per tenant covers all of that company's roles.
    Returns ``(None, None, None)`` for unrecognized hosts.
    """
    if not url:
        return (None, None, None)
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return (None, None, None)

    host = parts.netloc.lower()
    segments = [s for s in parts.path.split("/") if s]

    # boards.greenhouse.io/{tenant}/jobs/{id}  |  job-boards.greenhouse.io/...
    if "greenhouse.io" in host:
        tenant = segments[0] if segments else None
        job_id = None
        if "jobs" in segments:
            idx = segments.index("jobs")
            if idx + 1 < len(segments):
                job_id = segments[idx + 1]
        return ("greenhouse", tenant, job_id)

    # jobs.ashbyhq.com/{tenant}/{uuid}
    if "ashbyhq.com" in host:
        tenant = segments[0] if segments else None
        job_id = segments[1] if len(segments) > 1 else None
        return ("ashby", tenant, job_id)

    # jobs.lever.co/{tenant}/{id}
    if "lever.co" in host:
        tenant = segments[0] if segments else None
        job_id = segments[1] if len(segments) > 1 else None
        return ("lever", tenant, job_id)

    # {tenant}.wd{N}.myworkdayjobs.com/...
    if "myworkdayjobs.com" in host:
        tenant = host.split(".", 1)[0] or None
        return ("workday", tenant, segments[-1] if segments else None)

    # jobs.smartrecruiters.com/{company}/{id}
    if "smartrecruiters.com" in host:
        tenant = segments[0] if segments else None
        job_id = segments[1] if len(segments) > 1 else None
        return ("smartrecruiters", tenant, job_id)

    return ("other", None, None)


def _as_str_list(value: Any) -> list[str]:
    """Coerce a feed field to a clean list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _detect_remote(locations: list[str], title: str) -> bool:
    """True when the role advertises itself as remote."""
    return any(_REMOTE_RE.search(loc) for loc in locations) or bool(
        _REMOTE_RE.search(title)
    )


def _clean_terms(raw_terms: list[str]) -> list[str]:
    """Drop the ``N/A`` sentinel and de-duplicate, preserving order.

    Feeds do emit the same term twice in one array; downstream confidence
    scoring counts terms, so duplicates would skew it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for term in raw_terms:
        cleaned = _WS_RE.sub(" ", term).strip()
        if not cleaned or cleaned.upper() == "N/A":
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


# --- per-source normalizers ------------------------------------------------


def normalize_simplify(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    """Normalize a SimplifyJobs listing.

    Schema: source, category, company_name, id, title, active, terms,
    date_updated, date_posted, url, locations, company_url, is_visible,
    sponsorship, degrees. ``terms`` is an array of ``"<Season> <Year>"``.
    """
    if not isinstance(record, dict):
        return None
    external_id = record.get("id")
    company = record.get("company_name")
    title = record.get("title")
    apply_url = record.get("url")
    if not (external_id and company and title and apply_url):
        return None
    # is_visible=False means the aggregator has hidden the row upstream.
    if record.get("is_visible") is False:
        return None

    locations = _as_str_list(record.get("locations"))
    return ScoutRecord(
        source_id=source_id,
        external_id=str(external_id),
        company=str(company).strip(),
        title=str(title).strip(),
        apply_url=str(apply_url).strip(),
        company_url=(str(record["company_url"]).strip() if record.get("company_url") else None),
        locations=locations,
        terms=_clean_terms(_as_str_list(record.get("terms"))),
        category=(str(record["category"]).strip() if record.get("category") else None),
        sponsorship=(str(record["sponsorship"]).strip() if record.get("sponsorship") else None),
        degrees=_as_str_list(record.get("degrees")),
        is_remote=_detect_remote(locations, str(title)),
        active=bool(record.get("active", True)),
        date_posted=epoch_to_iso(record.get("date_posted")),
        date_updated=epoch_to_iso(record.get("date_updated")),
        raw=record,
    )


def normalize_vansh(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    """Normalize a vanshb03 listing.

    This feed's ``season`` is a bare season with **no year** (``"Fall"``, not
    ``"Fall 2027"``). A season without a year cannot be bucketed on its own, so
    ``terms`` is left empty and the record serves only as corroboration when its
    fingerprint matches a record from a source that does carry the year.
    """
    if not isinstance(record, dict):
        return None
    external_id = record.get("id")
    company = record.get("company_name")
    title = record.get("title")
    apply_url = record.get("url")
    if not (external_id and company and title and apply_url):
        return None
    if record.get("is_visible") is False:
        return None

    locations = _as_str_list(record.get("locations"))
    return ScoutRecord(
        source_id=source_id,
        external_id=str(external_id),
        company=str(company).strip(),
        title=str(title).strip(),
        apply_url=str(apply_url).strip(),
        company_url=(str(record["company_url"]).strip() if record.get("company_url") else None),
        locations=locations,
        terms=[],  # bare season, no year -> deliberately unusable for bucketing
        category=None,
        sponsorship=(str(record["sponsorship"]).strip() if record.get("sponsorship") else None),
        degrees=[],
        is_remote=_detect_remote(locations, str(title)),
        active=bool(record.get("active", True)),
        date_posted=epoch_to_iso(record.get("date_posted")),
        date_updated=epoch_to_iso(record.get("date_updated")),
        raw=record,
    )


def normalize_zshah(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    """Normalize a zshah101 listing.

    Schema differs again: ``company``/``title``/``season``/``seasons``, ISO
    ``posted_at`` (not epoch), and an explicit ``remote`` boolean. ``season``
    here *does* carry the year (e.g. ``"Summer 2027"``), so it feeds ``terms``.
    """
    if not isinstance(record, dict):
        return None
    external_id = record.get("id")
    company = record.get("company")
    title = record.get("title")
    apply_url = record.get("url")
    if not (external_id and company and title and apply_url):
        return None

    seasons = _as_str_list(record.get("seasons")) or _as_str_list(record.get("season"))
    # "Not stated" is this feed's null sentinel for season.
    seasons = [s for s in seasons if s.lower() != "not stated"]

    location = record.get("location")
    locations = _as_str_list(location)
    posted = record.get("posted_at") or record.get("first_seen_at")
    return ScoutRecord(
        source_id=source_id,
        external_id=str(external_id),
        company=str(company).strip(),
        title=str(title).strip(),
        apply_url=str(apply_url).strip(),
        company_url=None,
        locations=locations,
        terms=_clean_terms(seasons),
        category=(str(record["category"]).strip() if record.get("category") else None),
        sponsorship=(str(record["sponsorship"]).strip() if record.get("sponsorship") else None),
        degrees=[],
        is_remote=bool(record.get("remote")) or _detect_remote(locations, str(title)),
        active=True,  # this feed only publishes open roles
        date_posted=str(posted) if posted else None,
        date_updated=str(record["posted_at"]) if record.get("posted_at") else None,
        raw=record,
    )


def normalize_applyguy(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    """Normalize an ApplyGuy listing.

    ``url`` points at ApplyGuy's own site; ``listingUrl`` is the real ATS link,
    which is what enrichment and the apply button both need.

    ``season`` is free text and includes values that carry no usable term
    ("Not specified", "Co-op", "Year-round", a bare "2027"). Those are passed
    through as-is and rejected downstream by the term regex, which requires a
    season AND a year.
    """
    if not isinstance(record, dict):
        return None
    external_id = record.get("id")
    company = record.get("company")
    title = record.get("title")
    apply_url = record.get("listingUrl") or record.get("url")
    if not (external_id and company and title and apply_url):
        return None

    locations = _as_str_list(record.get("location"))
    season = record.get("season")
    return ScoutRecord(
        source_id=source_id,
        external_id=str(external_id),
        company=str(company).strip(),
        title=str(title).strip(),
        apply_url=str(apply_url).strip(),
        company_url=None,
        locations=locations,
        terms=_clean_terms(_as_str_list(season)),
        category=(str(record["category"]).strip() if record.get("category") else None),
        sponsorship=None,
        degrees=[],
        is_remote=_detect_remote(locations, str(title)),
        active=True,  # this feed only publishes open roles
        date_posted=(str(record["posted"]) if record.get("posted") else None),
        date_updated=(str(record["posted"]) if record.get("posted") else None),
        raw=record,
    )


# --- full-time search feeds ------------------------------------------------
#
# Unlike the GitHub lists above, these are search APIs over general job boards,
# queried with the user's own search terms. They carry full-time roles, which no
# internship list does.

_INTERN_TITLE_RE = re.compile(r"\b(intern(ship)?s?|co[- ]?op)\b", re.IGNORECASE)
_ENTRY_RE = re.compile(r"\b(entry[- ]level|junior|new ?grad|graduate)\b", re.IGNORECASE)


def _as_list(value: Any) -> list[str]:
    """These feeds encode lists as Python-repr strings: "['Senior', 'Mid-level']"."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            return [p.strip(" '\"") for p in text[1:-1].split(",") if p.strip(" '\"")]
        return [text] if text else []
    return []


def _employment_role(title: str, employment: str, level: str) -> str:
    """Decide a search result's role type. An intern title beats the feed's own
    employment type, because job boards mark many internships "Full Time"."""
    if _INTERN_TITLE_RE.search(title) or "intern" in employment.lower():
        return "internship"
    if _ENTRY_RE.search(level) or _ENTRY_RE.search(title):
        return "new_grad"
    return "full_time"


def _epoch_or_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def normalize_himalayas(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    title = (record.get("title") or "").strip()
    url = record.get("applicationLink") or record.get("guid")
    if not title or not url:
        return None

    expiry = record.get("expiryDate")
    active = True
    if expiry:
        try:
            active = int(expiry) > int(datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError):
            pass

    categories = _as_list(record.get("categories"))
    return ScoutRecord(
        source_id=source_id,
        external_id=str(record.get("guid") or url),
        company=(record.get("companyName") or "").strip(),
        title=title,
        apply_url=url,
        company_url=(
            f"https://himalayas.app/companies/{record['companySlug']}"
            if record.get("companySlug") else None
        ),
        locations=_as_list(record.get("locationRestrictions")) or ["Remote"],
        # Hyphenated slugs ("Data-Engineering") would never match the
        # taxonomy's feed_categories exactly.
        category=categories[0].replace("-", " ").lower() if categories else None,
        is_remote=True,
        active=active,
        date_posted=_epoch_or_iso(record.get("pubDate")),
        role_type=_employment_role(
            title,
            str(record.get("employmentType") or ""),
            " ".join(_as_list(record.get("seniority"))),
        ),
        raw=record,
    )


def normalize_jobicy(record: dict[str, Any], source_id: str) -> ScoutRecord | None:
    title = (record.get("jobTitle") or "").strip()
    # Jobicy's terms: application links must go to the URL the feed provides.
    url = record.get("url")
    if not title or not url:
        return None
    industries = _as_list(record.get("jobIndustry"))
    return ScoutRecord(
        source_id=source_id,
        external_id=str(record.get("id") or url),
        company=(record.get("companyName") or "").strip(),
        title=title,
        apply_url=url,
        company_url="https://jobicy.com",  # their terms ask for a credit link
        locations=[record.get("jobGeo")] if record.get("jobGeo") else ["Remote"],
        category=industries[0].lower() if industries else None,
        is_remote=True,
        date_posted=record.get("pubDate"),
        role_type=_employment_role(
            title,
            " ".join(_as_list(record.get("jobType"))),
            str(record.get("jobLevel") or ""),
        ),
        raw=record,
    )
