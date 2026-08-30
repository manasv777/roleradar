"""Academic terms, derived from the calendar rather than hardcoded.

The classifier this replaced held its target seasons as constants::

    BUCKET_SPRING_2027 = "spring_2027"
    BUCKET_SUMMER_2027 = "summer_2027"

which is correct for exactly one person for about a year. Everything here is
computed from a date instead, so the tool does not quietly stop matching
anything the moment its hardcoded seasons pass.

Three behaviours are carried over verbatim from the original because each one
encodes something measured against the live feeds, not a preference:

* ``_TERM_RE`` uses explicit lookarounds instead of ``\\b``. Workday writes
  ``"Software Engineering Co-op_Spring 2027"``, and ``_`` is a word character,
  so ``\\b`` never matches and those listings vanish silently.
* Winter and Spring of the same year collapse into one term. Both mean a
  January start, employers label them inconsistently, and in the live corpus
  Winter listings outnumbered Spring ones - separating them discards most of
  the cycle.
* The confidence ladder (0.95 / 0.70 / 0.30) demotes "evergreen" requisitions
  that list many terms at once, because those are rarely a real opening for any
  specific one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

__all__ = [
    "SEASONS",
    "Term",
    "canon_term",
    "effective_term_count",
    "horizon_terms",
    "resolve_term",
    "term_index",
]

SEASONS: tuple[str, ...] = ("winter", "spring", "summer", "fall")

# Nominal start month per season. Used only to order terms and to decide which
# ones are still ahead of the user - not to claim a real start date.
_SEASON_MONTH: dict[str, int] = {"winter": 1, "spring": 3, "summer": 6, "fall": 9}

# "autumn" is the same season under another name; feeds use both.
_SEASON_ALIASES: dict[str, str] = {"autumn": "fall"}

# Winter and Spring of one year are one cycle. Kept as a rule over season names
# rather than a hardcoded {"spring 2027", "winter 2027"} pair so it holds for
# every year without editing.
_COLLAPSE_INTO: dict[str, str] = {"winter": "spring"}

_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9])(spring|summer|fall|autumn|winter)"
    r"[\s_\-]*['‘’`]?\s*(?:20)?(\d{2})(?![0-9])",
    re.IGNORECASE,
)
# A season with no year at all. vanshb03's feed emits these; the original
# `_canon_term` could not express them and dropped the listing.
_BARE_SEASON_RE = re.compile(
    r"(?<![A-Za-z0-9])(spring|summer|fall|autumn|winter)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, order=True)
class Term:
    """One academic term, e.g. Summer 2027."""

    year: int
    month: int
    season: str

    def __init__(self, season: str, year: int) -> None:
        season = _SEASON_ALIASES.get(season.lower(), season.lower())
        if season not in SEASONS:
            raise ValueError(f"unknown season: {season!r}")
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "year", int(year))
        object.__setattr__(self, "month", _SEASON_MONTH[season])

    @property
    def id(self) -> str:
        """Stable identifier, e.g. ``"summer_2027"``."""
        return f"{self.season}_{self.year}"

    @property
    def label(self) -> str:
        """Human label. Collapsed terms name both halves so the merge is visible."""
        merged = [s for s, into in _COLLAPSE_INTO.items() if into == self.season]
        if merged:
            names = "–".join(s.capitalize() for s in (*merged, self.season))
            return f"{names} {self.year}"
        return f"{self.season.capitalize()} {self.year}"

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    def canonical(self) -> "Term":
        """The term this one collapses into (Winter 2027 -> Spring 2027)."""
        target = _COLLAPSE_INTO.get(self.season)
        return Term(target, self.year) if target else self


def canon_term(text: str) -> Term | None:
    """Parse a term out of free text, or return None.

    Applies the season collapse, so ``"Winter 2027"`` and ``"Spring 2027"``
    both come back as Spring 2027.
    """
    cleaned = _WS_RE.sub(" ", text or "")
    match = _TERM_RE.search(cleaned)
    if match:
        season, yy = match.group(1), int(match.group(2))
        # Two-digit years are this century; feeds have never used another.
        return Term(season, 2000 + yy).canonical()
    return None


def bare_season(text: str) -> str | None:
    """The season named in ``text`` when no year is given, else None.

    A feed that says only "Summer" cannot be placed on the calendar by itself,
    but it is still a usable signal once combined with the caller's horizon.
    """
    cleaned = _WS_RE.sub(" ", text or "")
    if _TERM_RE.search(cleaned):
        return None
    match = _BARE_SEASON_RE.search(cleaned)
    if not match:
        return None
    season = match.group(1).lower()
    return _SEASON_ALIASES.get(season, season)


def horizon_terms(today: date, months: int = 18) -> list[Term]:
    """Every term starting between ``today`` and ``months`` ahead, in order.

    Winter terms are omitted: they collapse into Spring, so emitting both would
    offer the user two names for one cycle.
    """
    end_year = today.year + (today.month + months - 1) // 12
    out: list[Term] = []
    for year in range(today.year, end_year + 2):
        for season in SEASONS:
            term = Term(season, year)
            if term.canonical() != term:
                continue
            months_ahead = (term.year - today.year) * 12 + (term.month - today.month)
            if 0 <= months_ahead <= months:
                out.append(term)
    return sorted(out)


def term_index(terms: Iterable[Term]) -> dict[str, Term]:
    """Map every accepted spelling to its Term, including collapsed spellings."""
    index: dict[str, Term] = {}
    for term in terms:
        index[f"{term.season} {term.year}"] = term
        for season, into in _COLLAPSE_INTO.items():
            if into == term.season:
                index[f"{season} {term.year}"] = term
        for alias, real in _SEASON_ALIASES.items():
            if real == term.season:
                index[f"{alias} {term.year}"] = term
    return index


def effective_term_count(terms: Iterable[Term]) -> int:
    """Count distinct cycles, so a Winter+Spring pair counts once."""
    return len({term.canonical() for term in terms})


def resolve_term(
    raw_terms: list[str],
    *,
    title: str = "",
    horizon: list[Term],
    preferred: list[str] | None = None,
) -> tuple[Term | None, float, list[str]]:
    """Pick the term a listing is for, with a confidence and a reason trace.

    Returns ``(term, confidence, reasons)``. ``term`` is None when nothing in
    the horizon matched; the caller decides what that means, rather than this
    function dropping the listing.
    """
    index = term_index(horizon)
    reasons: list[str] = []

    parsed = [t for t in (canon_term(raw) for raw in raw_terms or []) if t]

    if not parsed:
        recovered = canon_term(title)
        if recovered and recovered.id in {t.id for t in horizon}:
            return recovered, 0.55, [f"term recovered from the title: {recovered.label}"]
        season = bare_season(" ".join(raw_terms or []) or title)
        if season:
            upcoming = [t for t in horizon if t.canonical().season == season]
            if upcoming:
                return (
                    upcoming[0],
                    0.40,
                    [f"season {season!r} with no year - assumed {upcoming[0].label}"],
                )
        return None, 0.0, ["no term signal"]

    in_horizon = [t for t in parsed if t.id in {h.id for h in horizon}]
    if not in_horizon:
        names = ", ".join(sorted({t.label for t in parsed}))
        return None, 0.0, [f"terms present but outside the horizon: {names}"]

    # Prefer a term the user actually selected; otherwise the earliest upcoming
    # one, which is the soonest thing they could act on.
    chosen = None
    if preferred:
        wanted = [t for t in sorted(in_horizon) if t.id in set(preferred)]
        chosen = wanted[0] if wanted else None
    if chosen is None:
        chosen = sorted(in_horizon)[0]

    effective = effective_term_count(parsed)
    if effective == 1:
        confidence = 0.95
        reasons.append(f"single-term listing: {chosen.label}")
    elif effective <= 3:
        confidence = 0.70
        reasons.append(f"multi-term ({effective}): filed under {chosen.label}")
    else:
        confidence = 0.30
        reasons.append(f"evergreen requisition ({effective} terms) — demoted")

    return chosen, confidence, reasons
