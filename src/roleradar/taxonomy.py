"""Career-field taxonomy: domain -> specialty, loaded from config.

The classifier this replaced had three hardcoded tracks - ``swe``, ``ml_ai``,
``other`` - and threw away everything that landed in ``other`` at ingest time.
That is why a user could not "pick their field": there was one field, and it
was somebody else's.

Two decisions worth knowing:

**Multi-label.** A listing may carry several specialties. "ML Infrastructure
Engineer Intern" really is both ``ai_ml/ml_engineering`` and
``software/devops``, and a single label makes one of those a lie.

**Labelling, not filtering.** Nothing here drops a listing. An unmatched title
comes back with no labels and is still stored, so widening your interests later
is a re-classification rather than a re-fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Domain", "Specialty", "Taxonomy", "FieldVerdict", "load_taxonomy"]


@dataclass(frozen=True)
class Specialty:
    id: str
    label: str
    domain_id: str
    pattern: re.Pattern[str] | None
    skills: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Fully qualified id, e.g. ``"software/backend"``."""
        return f"{self.domain_id}/{self.id}"


@dataclass(frozen=True)
class Domain:
    id: str
    label: str
    feed_categories: frozenset[str]
    specialties: tuple[Specialty, ...]


@dataclass
class FieldVerdict:
    """What a listing was judged to be, and why."""

    domains: list[str] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    excluded: bool = False

    @property
    def matched(self) -> bool:
        return bool(self.domains) and not self.excluded


def _compile_any(patterns: list[str] | None) -> re.Pattern[str] | None:
    """One alternation per specialty - cheaper than N separate searches."""
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


class Taxonomy:
    """Compiled taxonomy. Build once, reuse for every listing in a run."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.version: int = int(raw.get("version", 1))
        self._exclude = _compile_any((raw.get("global_exclude") or {}).get("title_any"))

        domains: list[Domain] = []
        for d in raw.get("domains") or []:
            specialties = tuple(
                Specialty(
                    id=s["id"],
                    label=s.get("label", s["id"]),
                    domain_id=d["id"],
                    pattern=_compile_any(s.get("title_any")),
                    skills=tuple(s.get("skills") or ()),
                )
                for s in d.get("specialties") or []
            )
            domains.append(
                Domain(
                    id=d["id"],
                    label=d.get("label", d["id"]),
                    feed_categories=frozenset(
                        c.strip().lower() for c in (d.get("feed_categories") or [])
                    ),
                    specialties=specialties,
                )
            )
        self.domains: tuple[Domain, ...] = tuple(domains)

        self._by_domain = {d.id: d for d in self.domains}
        self._category_index: dict[str, str] = {}
        for d in self.domains:
            for category in d.feed_categories:
                self._category_index[category] = d.id

    # -- lookup ------------------------------------------------------------
    def domain(self, domain_id: str) -> Domain | None:
        return self._by_domain.get(domain_id)

    def all_specialty_keys(self) -> list[str]:
        return [s.key for d in self.domains for s in d.specialties]

    def skills_for(self, specialty_keys: list[str]) -> list[str]:
        """Seed skills for the chosen specialties, so onboarding is not blank."""
        wanted = set(specialty_keys)
        out: list[str] = []
        for d in self.domains:
            for s in d.specialties:
                if s.key in wanted:
                    out.extend(skill for skill in s.skills if skill not in out)
        return out

    # -- classification ----------------------------------------------------
    def classify(self, title: str, category: str | None = None) -> FieldVerdict:
        """Label a listing. Never drops it - an unmatched title is still a result."""
        verdict = FieldVerdict()
        title = title or ""

        if self._exclude and self._exclude.search(title):
            verdict.excluded = True
            verdict.reasons.append("title is not an engineering role")
            return verdict

        # The feed's own category is the strongest signal available: it is an
        # enumerated field the maintainers curate, not free text.
        domain_from_category = None
        if category:
            domain_from_category = self._category_index.get(category.strip().lower())
            if domain_from_category:
                verdict.domains.append(domain_from_category)
                verdict.confidence = 0.90
                verdict.reasons.append(f"feed category {category!r} -> {domain_from_category}")

        # Title regexes may add specialties, including in other domains: that is
        # what makes the labelling multi-label rather than a single winner.
        for d in self.domains:
            for s in d.specialties:
                if s.pattern and s.pattern.search(title):
                    if s.key not in verdict.specialties:
                        verdict.specialties.append(s.key)
                    if d.id not in verdict.domains:
                        verdict.domains.append(d.id)
                    verdict.reasons.append(f"title matches {s.key}")

        if verdict.specialties:
            verdict.confidence = max(verdict.confidence, 0.75)
        elif domain_from_category:
            # Domain is known but nothing narrower matched.
            verdict.specialties.append(f"{domain_from_category}/other")
            verdict.reasons.append("domain matched, no specialty pattern")

        if not verdict.domains:
            verdict.reasons.append("no domain matched")

        return verdict


@lru_cache(maxsize=4)
def load_taxonomy(path: str | None = None) -> Taxonomy:
    """Load and compile ``config/taxonomy.yml`` (or an explicit path)."""
    if path is None:
        from roleradar.settings import settings

        candidate = settings.config_dir / "taxonomy.yml"
        if not candidate.is_file():
            candidate = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yml"
        path = str(candidate)
    return Taxonomy(yaml.safe_load(Path(path).read_text()))
