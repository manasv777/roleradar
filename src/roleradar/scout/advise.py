"""Per-listing application advice: fit, logistics, priority, what to lead with.

This replaced automated resume drafting as Scout's output. The reason is
measured, not stylistic: on a sample listing an agent-written resume and a
local-model one scored *identically* (31.3), because the ATS composite is
bounded by required skills the candidate genuinely lacks (Go, C/C++, Linux) and
by JD phrases nothing can match ("commerce ads", "ad revenue"). Generating
prose could not move that. Telling the candidate **where he stands and what to
do about it** can.

Everything here is deterministic — no LLM call, so it costs nothing and runs on
every listing every time. Skill matching is alias-aware
(``skill_aliases.keyword_present``), so a profile that says "ML" satisfies a
posting that asks for "machine learning".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from roleradar.scout.skill_aliases import keyword_present, normalize

# Fit is judged RELATIVE to the listings actually available, not against an
# absolute bar. Measured across 261 enriched listings the distribution is
# median 0.27, p75 0.38 — so a fixed "0.70 = strong" rule labels essentially
# every real posting weak, which is true in the abstract and useless in
# practice. What the candidate needs to know is which of *these* postings are
# his best shots. Absolute counts ("4 of 13 met") are always shown alongside,
# so the relative framing never hides the raw truth.
FALLBACK_STRONG = 0.50
FALLBACK_WEAK = 0.25


def compute_benchmarks(fit_ratios: list[float]) -> tuple[float, float]:
    """Median and p75 of the current corpus, used to rank one listing in it."""
    values = sorted(f for f in fit_ratios if f is not None)
    if len(values) < 8:
        return FALLBACK_WEAK, FALLBACK_STRONG
    mid = values[len(values) // 2]
    p75 = values[int(len(values) * 0.75)]
    return mid, p75

# How each ATS behaves, so the advice is about *this* application, not generic.
ATS_NOTES: dict[str, str] = {
    "greenhouse": "Greenhouse: single form, parses a PDF resume cleanly. Optional cover-letter field.",
    "ashby": "Ashby: short form, good PDF parsing. Often asks a few custom screening questions.",
    "lever": "Lever: short form, clean PDF parsing. Frequently asks for links (GitHub, portfolio).",
    "workday": "Workday: long form and an account per employer. Its resume parser is the weakest — expect to hand-correct every field after upload, and budget 15-20 minutes.",
    "smartrecruiters": "SmartRecruiters: medium-length form, reliable PDF parsing.",
}


@dataclass
class Advice:
    listing_id: str
    fit_ratio: float = 0.0
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    verdict: str = ""
    logistics: list[str] = field(default_factory=list)
    emphasize: list[str] = field(default_factory=list)
    priority: float = 0.0
    urgency: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fit_ratio": round(self.fit_ratio, 2),
            "matched": self.matched,
            "missing": self.missing,
            "verdict": self.verdict,
            "logistics": self.logistics,
            "emphasize": self.emphasize,
            "priority": round(self.priority, 3),
            "urgency": self.urgency,
        }



# Skill names this short are ordinary English words ("Go", "R", "C"), so
# matching them against free prose produces confident nonsense — an early run
# reported a project "covers Go" because the word appeared in a sentence.
# These are only credited when they appear in the explicit skills list.
_AMBIGUOUS_MAX_LEN = 2

# Job descriptions state requirements as alternatives far more often than as
# single skills: "TensorFlow/PyTorch/MXNet" or "at least one of (Go, Java,
# Python, C++)". Treating the whole string as one term marks a candidate who
# has PyTorch and Python as missing both.
_PAREN_LIST = re.compile(r"\(([^)]*,[^)]*)\)")
_PAREN_SINGLE = re.compile(r"\(([^),]{2,40})\)")

# Alternatives are only credible when each side names something. A part that is
# a bare qualifier ("current", "former", "strong") matches ordinary prose and
# turns an eligibility clause into a false skill match.
_GENERIC_PART = {
    "current", "former", "previous", "prior", "strong", "solid", "good",
    "excellent", "basic", "advanced", "familiar", "experience", "knowledge",
    "ability", "understanding", "proficiency", "other", "related", "similar",
}

# Requirements phrased as eligibility or disposition are not skills, and
# pretending to score them produces confident nonsense in both directions.
_NON_SKILL_MARKERS = (
    "intern at", "former", "current/", "must be", "eligible", "authorization",
    "citizen", "clearance", "willing", "able to commit", "enrolled",
    "problem-solving", "self-directed", "communication skills", "team player",
    "attention to detail", "work ethic", "passion", "writing skills",
    "computer literacy", "detail-oriented", "detail oriented", "interpersonal",
    "organizational skills", "time management", "leadership skills",
    "verbal", "presentation skills", "critical thinking", "collaborative",
    "fast-paced", "motivated", "curious", "adaptab",
)


def is_scoreable(requirement: str) -> bool:
    """Whether a stated requirement is a skill we can honestly check."""
    low = (requirement or "").lower()
    if not low.strip():
        return False
    return not any(m in low for m in _NON_SKILL_MARKERS)


def requirement_terms(requirement: str) -> list[str]:
    """Split a stated requirement into the terms that would each satisfy it."""
    text = (requirement or "").strip()
    if not text:
        return []
    single = _PAREN_SINGLE.search(text)
    if single and not _PAREN_LIST.search(text):
        # "version control (Git)" — either the general phrase or the concrete
        # tool named in the parenthetical satisfies it.
        head = text[: single.start()].strip(" .")
        inner_term = single.group(1).strip()
        return [t for t in (inner_term, head) if t and normalize(t) not in _GENERIC_PART]
    inner = _PAREN_LIST.search(text)
    if inner:
        parts = [p.strip(" .") for p in inner.group(1).split(",")]
        terms = [p for p in parts if p and normalize(p) not in _GENERIC_PART]
        if terms:
            return terms
    # A/B/C alternatives, but not a genuine name that contains a slash.
    if "/" in text and normalize(text) not in {"ci/cd", "tcp/ip", "a/b"}:
        parts = [
            p.strip() for p in text.split("/")
            if len(p.strip()) > 1 and normalize(p.strip()) not in _GENERIC_PART
        ]
        if len(parts) > 1:
            return parts
    return [text]


def requirement_met(requirement: str, prose: str, skills: list[str]) -> bool:
    """Whether the candidate satisfies a requirement under any of its terms."""
    skills_blob = " , ".join(skills)
    for term in requirement_terms(requirement):
        # Short, ambiguous names are credited ONLY from the explicit skills
        # list; everything else may be evidenced anywhere in the profile.
        haystack = (
            skills_blob
            if len(term.strip()) <= _AMBIGUOUS_MAX_LEN
            else f"{prose} , {skills_blob}"
        )
        if keyword_present(term, haystack):
            return True
    return False


def _required_skills(job_keywords: dict[str, Any]) -> list[str]:
    out, seen = [], set()
    for key in ("required_skills", "preferred_skills"):
        for v in job_keywords.get(key) or []:
            if isinstance(v, str) and v.strip() and normalize(v) not in seen:
                seen.add(normalize(v))
                out.append(v.strip())
    return out


def _profile_text(master: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    for key in ("summary", "workExperience", "personalProjects", "education", "additional"):
        walk(master.get(key))
    return " ".join(parts)


def _days_until(deadline: str | None) -> int | None:
    if not deadline:
        return None
    try:
        due = datetime.fromisoformat(str(deadline))
    except (TypeError, ValueError):
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (due - datetime.now(timezone.utc)).days


def _age_days(first_seen: str | None) -> int | None:
    if not first_seen:
        return None
    try:
        seen = datetime.fromisoformat(str(first_seen))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).days


def best_evidence(master: dict[str, Any], matched: list[str], limit: int = 3) -> list[str]:
    """Which entries to lead with — the ones actually carrying the JD's skills.

    Ranked by how many of the matched requirements each entry demonstrates, so
    the suggestion is grounded in the profile rather than a guess about what
    sounds impressive.
    """
    scored: list[tuple[int, str]] = []
    for key, name_field in (("workExperience", "company"), ("personalProjects", "name")):
        for entry in master.get(key) or []:
            blob = " ".join(
                [str(entry.get(name_field) or ""), str(entry.get("title") or "")]
                + [b for b in (entry.get("description") or []) if isinstance(b, str)]
            )
            hits = [s for s in matched if requirement_met(s, blob, [])]
            if hits:
                label = str(entry.get(name_field) or "").strip()
                scored.append((len(hits), f"{label} — covers {', '.join(hits[:3])}"))
    scored.sort(key=lambda x: -x[0])
    return [text for _, text in scored[:limit]]


def advise(
    listing: dict[str, Any],
    master: dict[str, Any],
    job_keywords: dict[str, Any],
    *,
    duplicate_locations: int = 1,
    benchmarks: tuple[float, float] | None = None,
) -> Advice:
    """Build the full advice payload for one listing. Pure function, no I/O."""
    a = Advice(listing_id=listing.get("listing_id", ""))
    profile = _profile_text(master)

    skills = [s for s in ((master.get("additional") or {}).get("technicalSkills") or [])
              if isinstance(s, str)]
    required = [r for r in _required_skills(job_keywords) if is_scoreable(r)]
    if required:
        a.matched = [s for s in required if requirement_met(s, profile, skills)]
        a.missing = [s for s in required if s not in a.matched]
        a.fit_ratio = len(a.matched) / len(required)

    # --- verdict -----------------------------------------------------------
    median, p75 = benchmarks or (FALLBACK_WEAK, FALLBACK_STRONG)
    met = f"{len(a.matched)} of {len(required)} stated requirements met"
    if not required:
        a.verdict = "No requirements extracted yet — fit unknown."
    elif len(required) < 3:
        a.verdict = (
            f"Only {len(required)} requirement(s) published — {met}, "
            "but that is too little to judge fit. Read the posting."
        )
    elif a.fit_ratio >= p75:
        a.verdict = f"Among your strongest matches — {met}."
    elif a.fit_ratio >= median:
        a.verdict = (
            f"Above average for your profile — {met}. "
            f"Address {', '.join(a.missing[:2])} directly rather than hoping it goes unnoticed."
        )
    else:
        a.verdict = (
            f"Below average for your profile — {met}. "
            "Worth applying only after the stronger matches."
        )

    # --- logistics ---------------------------------------------------------
    vendor = (listing.get("ats_vendor") or "").lower()
    if vendor in ATS_NOTES:
        a.logistics.append(ATS_NOTES[vendor])
    days = _days_until(listing.get("deadline"))
    if days is not None:
        a.logistics.append(
            f"Deadline in {days} day(s)." if days >= 0 else "Deadline has passed — verify before applying."
        )
    else:
        a.logistics.append("No deadline published — treat as rolling and apply early.")
    if duplicate_locations > 1:
        a.logistics.append(
            f"Posted in {duplicate_locations} locations — this is ONE application, "
            "not several. Pick the location you want."
        )
    if listing.get("needs_verification"):
        a.logistics.append(
            "Work-authorization requirement is INFERRED, never confirmed by the feed. "
            "Verify on the posting before spending time on it."
        )

    # --- what to lead with -------------------------------------------------
    a.emphasize = best_evidence(master, a.matched)
    if a.missing:
        a.emphasize.append(
            f"Gap to address: {', '.join(a.missing[:3])} — name it honestly or show the nearest thing you have."
        )

    # --- priority ----------------------------------------------------------
    # Deadline dominates: a closing posting cannot wait for a better-fitting one.
    urgency = 0.0
    if days is not None:
        urgency = 1.0 if days <= 7 else 0.6 if days <= 30 else 0.3
        a.urgency = "closing" if days <= 7 else "dated"
    age = _age_days(listing.get("first_seen_at"))
    freshness = 0.3 if age is not None and age <= 2 else 0.0
    if freshness and not a.urgency:
        a.urgency = "new"
    # Confidence: a posting stating one requirement tells us far less than one
    # stating eight, so a 1-of-1 "perfect fit" must not outrank a solid 5-of-8.
    confidence = min(1.0, len(required) / 5.0) if required else 0.0
    a.priority = urgency * 2.0 + a.fit_ratio * confidence + freshness
    return a
