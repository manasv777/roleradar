"""Bucket, track, and work-authorization classification.

Every classifier here is **pure Python with zero LLM calls**. That is a
deliberate cost decision and it costs nothing in quality: each signal the
classifiers read (``terms``, ``category``, ``sponsorship``, ``locations``) is
an enumerated field in the source feeds, not free text needing interpretation.

The one genuinely uncertain judgement — whether a role can be taken without US
work authorization — is scored, not decided. It carries a confidence, a
human-readable reason trace, and an unconditional ``needs_verification`` flag,
because no feed publishes that fact and a wrong answer is expensive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from roleradar.scout.normalize import ScoutRecord

# --- buckets ---------------------------------------------------------------

BUCKET_SPRING_2027 = "spring_2027"
BUCKET_SUMMER_2027 = "summer_2027"
BUCKET_FALL_2026_NO_AUTH = "fall_2026_no_auth"

TRACK_SWE = "swe"
TRACK_ML_AI = "ml_ai"
TRACK_OTHER = "other"

# Canonical season+year -> internal term bucket.
#
# Winter 2027 and Spring 2027 collapse into ONE bucket on purpose: both mean a
# January/Q1 start, companies label them inconsistently, and in the live feed
# there are more active Winter 2027 roles than Spring 2027 ones. Treating them
# separately would silently discard the majority of the bucket.
_TERM_BUCKETS: dict[str, str] = {
    "spring 2027": BUCKET_SPRING_2027,
    "winter 2027": BUCKET_SPRING_2027,
    "summer 2027": BUCKET_SUMMER_2027,
    "fall 2026": "fall_2026",
    "autumn 2026": "fall_2026",
}

# The Winter/Spring pair counts as a single "effective" term for the confidence
# ladder, so a genuine single-cycle January req is not demoted as multi-term.
_EQUIVALENT_TERM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"spring 2027", "winter 2027"}),
)

# Note the explicit lookarounds instead of \b: Workday writes titles like
# "Software Engineering Co-op_Spring 2027", and `_` is a word character, so \b
# would never match there and those listings would be silently dropped.
_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9])(spring|summer|fall|autumn|winter)"
    r"[\s_\-]*[''`]?\s*(?:20)?(\d{2})(?![0-9])",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Verdict:
    """The classification outcome for one record."""

    bucket: str | None
    confidence: float
    track: str | None
    needs_verification: bool
    reasons: list[str] = field(default_factory=list)


def _canon_term(term: str) -> str | None:
    """Normalize a term string to ``"<season> <yyyy>"``, or ``None``."""
    match = _TERM_RE.search(_WS_RE.sub(" ", term or ""))
    if not match:
        return None
    season = match.group(1).lower()
    year = int(match.group(2))
    return f"{season} {2000 + year}"


def _effective_term_count(canon_terms: list[str]) -> int:
    """Count terms, collapsing known-equivalent seasons into one.

    Winter 2027 + Spring 2027 on the same posting is one January cycle, not
    two, so it must not be penalized by the multi-term demotion.
    """
    remaining = set(canon_terms)
    count = 0
    for group in _EQUIVALENT_TERM_GROUPS:
        overlap = remaining & group
        if overlap:
            count += 1
            remaining -= overlap
    return count + len(remaining)


def bucket_terms(
    terms: list[str], *, title: str = ""
) -> tuple[str | None, float, list[str]]:
    """Map a record's terms to ``(term_bucket, confidence, reasons)``.

    Confidence encodes how much the posting looks like a genuine single-cycle
    opening rather than an evergreen requisition that lists every term a
    company will ever hire for.
    """
    reasons: list[str] = []
    canon = [c for c in (_canon_term(t) for t in terms) if c]

    if not canon:
        # No usable term in the feed — try the title as a fallback. Only ~1.4%
        # of term-less records are recoverable this way, but they are free.
        recovered = _canon_term(title)
        if recovered and recovered in _TERM_BUCKETS:
            reasons.append(f"term absent from feed; recovered from title: {recovered}")
            return (_TERM_BUCKETS[recovered], 0.55, reasons)
        return (None, 0.0, ["no term signal"])

    matched = [c for c in canon if c in _TERM_BUCKETS]
    if not matched:
        return (None, 0.0, [f"terms present but none in scope: {', '.join(canon)}"])

    # A posting can span several in-scope buckets (e.g. Fall 2026 + Winter
    # 2027). Prefer a bucket the user is actually authorized to work in — a
    # role reachable as a Spring 2027 internship should never be filed under
    # the no-work-authorization bucket just because Fall came first in the
    # array.
    candidates = {_TERM_BUCKETS[m] for m in matched}
    bucket = next(
        b for b in (BUCKET_SPRING_2027, BUCKET_SUMMER_2027, "fall_2026") if b in candidates
    )
    effective = _effective_term_count(canon)

    if effective == 1:
        confidence = 0.95
        reasons.append(f"single-term listing: {matched[0]}")
    elif effective <= 3:
        confidence = 0.70
        reasons.append(f"multi-term ({effective}): {', '.join(canon)}")
    else:
        confidence = 0.30
        reasons.append(f"evergreen requisition ({effective} terms) — demoted")

    if len(candidates) > 1:
        reasons.append(
            f"spans multiple in-scope buckets ({', '.join(sorted(candidates))}); "
            f"filed under {bucket} — the one you can work in"
        )
    return (bucket, confidence, reasons)


# --- track ----------------------------------------------------------------

# Each feed names its categories differently, so every spelling in use is
# mapped explicitly. An unmapped value silently falls through to title
# guessing, which is how zshah's 64 "Data & ML/AI" records were being
# classified before this table covered them.
_CATEGORY_TRACKS: dict[str, str] = {
    # SimplifyJobs
    "software": TRACK_SWE,
    "software engineering": TRACK_SWE,
    "ai/ml/data": TRACK_ML_AI,
    "data science, ai & machine learning": TRACK_ML_AI,
    # zshah101
    "data & ml/ai": TRACK_ML_AI,
    "security": TRACK_SWE,
    # Out of scope, mapped so they are rejected deliberately rather than
    # falling through to a title guess.
    "quant": TRACK_OTHER,
    "hardware": TRACK_OTHER,
    "product": TRACK_OTHER,
}

# Data-analyst style roles sit under AI/ML/Data upstream but are not ML work.
_NOT_ML_RE = re.compile(
    r"\b(data analyst|business intelligence|bi analyst|analytics analyst|"
    r"business analyst|financial analyst)\b",
    re.IGNORECASE,
)
# Non-engineering roles that occasionally land in the Software category.
_NOT_SWE_RE = re.compile(
    r"\b(sales|recruit\w*|marketing|account executive|customer success|"
    r"technical writer|community manager)\b",
    re.IGNORECASE,
)
_ML_TITLE_RE = re.compile(
    r"\b(machine learning|deep learning|\bml\b|\bai\b|artificial intelligence|"
    r"research scientist|applied scientist|nlp|computer vision|llm)\b",
    re.IGNORECASE,
)


def classify_track(category: str | None, title: str) -> tuple[str | None, list[str]]:
    """Map a record to ``swe`` / ``ml_ai`` / ``other`` with a reason trace."""
    reasons: list[str] = []
    key = (category or "").strip().lower()
    track = _CATEGORY_TRACKS.get(key)

    if track is None:
        # No category (e.g. the university feed) — fall back to the title.
        if _ML_TITLE_RE.search(title):
            return (TRACK_ML_AI, ["no category; ML/AI inferred from title"])
        if re.search(r"\b(software|engineer|developer|programming)\b", title, re.I):
            return (TRACK_SWE, ["no category; SWE inferred from title"])
        return (TRACK_OTHER, ["no category and no track signal in title"])

    reasons.append(f"category '{category}' -> {track}")

    if track == TRACK_ML_AI and _NOT_ML_RE.search(title):
        return (TRACK_OTHER, reasons + ["title reads as a data/business analyst role"])
    if track == TRACK_SWE and _NOT_SWE_RE.search(title):
        return (TRACK_OTHER, reasons + ["title reads as a non-engineering role"])
    return (track, reasons)


# --- work authorization ----------------------------------------------------

_US_STATE_CODES = frozenset(
    """AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS
    MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI
    WY DC PR""".split()
)
_US_ALIASES = frozenset({"nyc", "sf", "usa", "united states", "u.s.", "us", "remote in usa"})

_NO_AUTH_TITLE_RE = re.compile(
    r"\b(fellowship|fellow|mentorship|mentee|research assistant|open source|"
    r"contributor|volunteer|apprentice\w*|grant|scholarship|bounty)\b",
    re.IGNORECASE,
)

SPONSORSHIP_CITIZENSHIP = "U.S. Citizenship is Required"
SPONSORSHIP_NONE = "Does Not Offer Sponsorship"
SPONSORSHIP_OFFERS = "Offers Sponsorship"

# A curated-registry entry is human-verified, so it alone clears the threshold.
NO_AUTH_THRESHOLD = 0.55
NO_AUTH_LOW_CONFIDENCE_FLOOR = 0.35


def _location_is_us(location: str) -> bool | None:
    """True/False if the location is clearly US/non-US, ``None`` if unclear."""
    text = location.strip()
    if not text:
        return None
    if text.lower() in _US_ALIASES:
        return True
    tail = [part.strip() for part in text.split(",")][-1]
    if tail.upper() in _US_STATE_CODES:
        return True
    if tail.lower() in _US_ALIASES:
        return True
    # A trailing country name that is not the US.
    if len(tail) > 2 and tail.upper() not in _US_STATE_CODES:
        return False
    return None


def score_no_us_auth(record: ScoutRecord) -> tuple[float, list[str]]:
    """Score how likely a role is doable without US work authorization.

    Additive weights, clamped to ``[0, 1]``. This is a heuristic over data that
    does not encode the answer — treat the output as a shortlist signal, never
    as a determination. Callers must set ``needs_verification`` regardless of
    the score.
    """
    score = 0.0
    reasons: list[str] = []

    if record.source_id == "curated_programs":
        score += 0.60
        reasons.append("curated program registry entry — human-verified")

    if _NO_AUTH_TITLE_RE.search(record.title):
        score += 0.30
        reasons.append("title matches fellowship/research/open-source lexicon")

    verdicts = [_location_is_us(loc) for loc in record.locations]
    known = [v for v in verdicts if v is not None]
    if known and not any(known):
        score += 0.35
        reasons.append(f"all locations outside the US: {', '.join(record.locations)}")
    elif any(v is True for v in verdicts):
        score -= 0.35
        reasons.append("US-based location — requires US work authorization")

    if record.is_remote:
        score += 0.20
        reasons.append("advertised as remote")

    sponsorship = (record.sponsorship or "").strip()
    if sponsorship == SPONSORSHIP_CITIZENSHIP:
        # Hard veto: no combination of positive signals can rescue this.
        reasons.append("VETO: US citizenship required")
        return (0.0, reasons)
    if sponsorship == SPONSORSHIP_NONE:
        score -= 0.45
        reasons.append("employer does not offer sponsorship")
    elif sponsorship == SPONSORSHIP_OFFERS:
        score += 0.15
        reasons.append("employer offers sponsorship")

    return (max(0.0, min(1.0, score)), reasons)


# --- top level -------------------------------------------------------------


def classify(record: ScoutRecord) -> Verdict:
    """Classify one record into a bucket, track, and confidence.

    Returns ``bucket=None`` for anything out of scope; callers drop those
    rather than persisting them (which is also what keeps the licensing posture
    clean — only the filtered shortlist is ever stored).
    """
    term_bucket, confidence, reasons = bucket_terms(record.terms, title=record.title)
    track, track_reasons = classify_track(record.category, record.title)
    reasons = list(reasons) + track_reasons

    if term_bucket is None:
        return Verdict(None, 0.0, track, False, reasons)

    if track == TRACK_OTHER:
        return Verdict(None, 0.0, track, False, reasons + ["track out of scope"])

    # Spring/Summer 2027: the user has work authorization, so the term bucket
    # is the answer and no authorization scoring applies.
    if term_bucket in (BUCKET_SPRING_2027, BUCKET_SUMMER_2027):
        return Verdict(term_bucket, confidence, track, False, reasons)

    # Fall 2026: the user will NOT have work authorization, so a role only
    # qualifies if it plausibly needs none. Always flagged for verification.
    if term_bucket == "fall_2026":
        auth_score, auth_reasons = score_no_us_auth(record)
        reasons = reasons + auth_reasons
        if auth_score < NO_AUTH_LOW_CONFIDENCE_FLOOR:
            return Verdict(None, 0.0, track, False, reasons)
        # Blend: how sure we are of the term AND of the authorization read.
        combined = round(min(confidence, auth_score), 4)
        return Verdict(
            BUCKET_FALL_2026_NO_AUTH,
            combined,
            track,
            True,  # unconditional — the classifier is never authoritative here
            reasons,
        )

    return Verdict(None, 0.0, track, False, reasons)
