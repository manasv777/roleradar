"""Classify a listing: which term, what kind of role, which fields.

Every classifier here is pure Python with zero LLM calls. That is a deliberate
cost decision and it costs nothing in quality: each signal read (`terms`,
`category`, `sponsorship`, `locations`) is an enumerated field in the source
feeds, not free text needing interpretation.

What changed from the original, and why:

* Seasons are no longer constants. `terms.py` derives them from the calendar,
  so the tool does not stop matching anything once a hardcoded year passes.
* Fields are multi-label and come from `config/taxonomy.yml`, so a user picks
  their own rather than inheriting `swe`/`ml_ai`/`other`.
* Work authorization is a user preference, not an assumption. The original
  hardcoded that its author could work some terms and not others; now the whole
  scoring path is skipped unless the user says they need sponsorship.
* Nothing is dropped here. `classify` labels; `store.py` decides what to keep.
  Filtering at ingest is what made adding a field later require a re-fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from roleradar.prefs import Preferences
from roleradar.scout.normalize import ScoutRecord
from roleradar.taxonomy import Taxonomy
from roleradar.terms import Term, resolve_term

ROLE_INTERNSHIP = "internship"
ROLE_NEW_GRAD = "new_grad"
ROLE_FULL_TIME = "full_time"
ROLE_UNKNOWN = "unknown"

@dataclass
class Verdict:
    """What a listing is, and the trace explaining why.

    Multi-label by design: an "ML Infrastructure Engineer" is genuinely both an
    ML and an infrastructure role, and forcing one label makes the other wrong.

    `term` is None when nothing in the user's horizon matched. That is a label,
    not a verdict to discard the row - new-grad postings legitimately carry no
    term at all.
    """

    term: Term | None = None
    role_type: str = ROLE_UNKNOWN
    domains: list[str] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)
    confidence: float = 0.0
    needs_verification: bool = False
    work_auth_score: float | None = None
    reasons: list[str] = field(default_factory=list)
    excluded: bool = False

    @property
    def term_id(self) -> str | None:
        return self.term.id if self.term else None


# --- role type -------------------------------------------------------------
#
# New-grad postings carry no `terms` field at all, so the original classifier
# returned "no bucket" for every one of them and `store.py` discarded the lot.
# Supporting them is a real classifier, not a flag.

_INTERNSHIP_RE = re.compile(
    r"\b(intern(ship)?s?|co[- ]?op|summer analyst|industrial placement)\b", re.IGNORECASE
)
_NEW_GRAD_RE = re.compile(
    r"\b(new ?grad(uate)?|university (grad(uate)?|hire)|entry[- ]level|early career|"
    r"campus hire|graduate (engineer|programme|program|scheme)|rotational program)\b",
    re.IGNORECASE,
)
# A senior title is neither, and saying so keeps company boards (which list
# every level) from flooding an early-career feed.
_SENIOR_RE = re.compile(
    r"\b(senior|staff|principal|lead|manager|director|head of|vp|architect|"
    r"sr\.?|iii|iv)\b",
    re.IGNORECASE,
)


def classify_role_type(
    title: str, *, source_role_type: str | None = None, has_term: bool = False
) -> tuple[str, list[str]]:
    """Decide whether a listing is an internship, a new-grad role, or neither."""
    title = title or ""

    if source_role_type in (ROLE_INTERNSHIP, ROLE_NEW_GRAD, ROLE_FULL_TIME):
        return source_role_type, [f"source declares {source_role_type}"]

    if _INTERNSHIP_RE.search(title):
        return ROLE_INTERNSHIP, ["title names an internship or co-op"]
    if _NEW_GRAD_RE.search(title):
        return ROLE_NEW_GRAD, ["title names a new-grad or entry-level role"]
    if _SENIOR_RE.search(title):
        # A senior title is a full-time role, not an unknown one. It used to be
        # filed as unknown because the tool only served early-career searches;
        # with full-time roles selectable, calling it unknown hides it.
        return ROLE_FULL_TIME, ["title reads as a senior full-time role"]
    if has_term:
        return ROLE_INTERNSHIP, ["carries an academic term, so treated as an internship"]
    return ROLE_UNKNOWN, ["no role-type signal in the title"]


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


def classify(
    record: ScoutRecord,
    *,
    prefs: Preferences,
    taxonomy: Taxonomy,
    horizon: list[Term],
) -> Verdict:
    """Label one record. Never drops it.

    The original returned ``bucket=None`` for anything out of scope and callers
    threw those rows away, which meant adding a field later required refetching
    the world. This labels instead: `store.py` owns the keep/discard decision,
    and an unlabelled row is still queryable once the user widens their
    interests.
    """
    reasons: list[str] = []

    term, term_confidence, term_reasons = resolve_term(
        record.terms,
        title=record.title,
        horizon=horizon,
        preferred=prefs.explicit_terms or None,
    )
    reasons += term_reasons

    role_type, role_reasons = classify_role_type(
        record.title,
        source_role_type=getattr(record, "role_type", None),
        has_term=term is not None,
    )
    reasons += role_reasons

    fields = taxonomy.classify(record.title, record.category)
    reasons += fields.reasons

    verdict = Verdict(
        term=term,
        role_type=role_type,
        domains=list(fields.domains),
        specialties=list(fields.specialties),
        # Term confidence is what the ladder measured; where there is no term
        # (a new-grad role), the field match is the only thing we know.
        confidence=round(max(term_confidence, fields.confidence if term is None else term_confidence), 4),
        excluded=fields.excluded,
        reasons=reasons,
    )

    # Work authorization is scored only when the user says they need it. For
    # everyone else this path costs nothing and claims nothing.
    if prefs.work_auth.requires_sponsorship:
        auth_score, auth_reasons = score_no_us_auth(record)
        verdict.work_auth_score = auth_score
        verdict.reasons += auth_reasons
        # The classifier is never authoritative about visa status: no feed
        # publishes it, so this is a shortlist signal and must say so.
        verdict.needs_verification = True
        if not prefs.work_auth.citizen_only_ok and auth_score == 0.0:
            verdict.reasons.append("employer states citizenship is required")

    return verdict
