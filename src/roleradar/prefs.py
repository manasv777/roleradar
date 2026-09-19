"""User preferences: what to scout for, and for whom.

Everything that used to be a constant in the classifier lives here instead -
which fields, which terms, whether sponsorship is needed. `settings.py` already
advertised `data/preferences.json`; this is the module that actually reads it.

The file is plain JSON and safe to hand-edit. Anything missing falls back to a
default, so a partial file is valid rather than an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from roleradar.terms import Term, horizon_terms

__all__ = ["Preferences", "WorkAuth", "load_prefs", "save_prefs", "DEFAULT_PREFERENCES"]

PREFERENCES_VERSION = 1


@dataclass
class WorkAuth:
    """Whether the user needs visa sponsorship.

    This replaces an assumption that was compiled into the classifier - that
    the user could work some terms and not others. For most people
    `requires_sponsorship` is False and the whole scoring path is skipped.
    """

    requires_sponsorship: bool = False
    # False vetoes any listing that states citizenship is required.
    citizen_only_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "requires_sponsorship": self.requires_sponsorship,
            "citizen_only_ok": self.citizen_only_ok,
        }


@dataclass
class Preferences:
    version: int = PREFERENCES_VERSION
    # domain id -> specialty ids. An empty list means "the whole domain".
    interests: dict[str, list[str]] = field(default_factory=dict)
    role_types: list[str] = field(default_factory=lambda: ["internship", "new_grad"])
    # Search terms for full-time roles, used only when role_types includes
    # "full_time". Empty means "derive them from my chosen fields" - a fixed
    # default here would be one person's job search shipped to everyone.
    full_time_searches: list[str] = field(default_factory=list)
    term_mode: str = "auto"                     # auto | explicit
    horizon_months: int = 18
    explicit_terms: list[str] = field(default_factory=list)
    work_auth: WorkAuth = field(default_factory=WorkAuth)
    locations: list[str] = field(default_factory=list)
    exclude_countries: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    notify_enabled: bool = True
    notify_min_confidence: float = 0.70
    notify_max_per_run: int = 5
    schedule_enabled: bool = True
    interval_minutes: int = 30
    company_interval_minutes: int = 360
    store_scope: str = "taxonomy"               # all | taxonomy | selected
    prune_inactive_after_days: int = 180

    # -- derived -----------------------------------------------------------
    def selected_specialties(self) -> list[str]:
        """Fully qualified specialty keys the user cares about.

        A domain chosen with no specialties means every specialty in it, which
        is what "I want all of Software" should mean.
        """
        out: list[str] = []
        for domain_id, specialties in self.interests.items():
            if not specialties:
                out.append(f"{domain_id}/*")
            else:
                out.extend(
                    s if "/" in s else f"{domain_id}/{s}" for s in specialties
                )
        return out

    def wants(self, specialty_keys: list[str], domains: list[str]) -> bool:
        """Does this listing match the user's interests?

        With no interests recorded yet, everything matches - a fresh install
        should show results rather than an empty page.
        """
        if not self.interests:
            return True
        for key in self.selected_specialties():
            if key.endswith("/*"):
                if key[:-2] in domains:
                    return True
            elif key in specialty_keys:
                return True
        return False

    def horizon(self, today: date | None = None) -> list[Term]:
        """The terms to look for, from the calendar or from an explicit list."""
        today = today or date.today()
        if self.term_mode == "explicit" and self.explicit_terms:
            out: list[Term] = []
            for term_id in self.explicit_terms:
                season, _, year = term_id.partition("_")
                try:
                    out.append(Term(season, int(year)))
                except (ValueError, TypeError):
                    continue
            if out:
                return sorted(out)
        return horizon_terms(today, months=self.horizon_months)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "interests": self.interests,
            "role_types": self.role_types,
            "full_time": {"searches": self.full_time_searches},
            "terms": {
                "mode": self.term_mode,
                "horizon_months": self.horizon_months,
                "explicit": self.explicit_terms,
            },
            "work_auth": self.work_auth.to_dict(),
            "locations": {
                "preferred": self.locations,
                "exclude_countries": self.exclude_countries,
            },
            "skills": self.skills,
            "highlights": self.highlights,
            "notifications": {
                "enabled": self.notify_enabled,
                "min_confidence": self.notify_min_confidence,
                "max_per_run": self.notify_max_per_run,
            },
            "schedule": {
                "enabled": self.schedule_enabled,
                "interval_minutes": self.interval_minutes,
                "company_interval_minutes": self.company_interval_minutes,
            },
            "storage": {
                "store_scope": self.store_scope,
                "prune_inactive_after_days": self.prune_inactive_after_days,
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Preferences":
        terms = raw.get("terms") or {}
        auth = raw.get("work_auth") or {}
        locations = raw.get("locations") or {}
        notify = raw.get("notifications") or {}
        schedule = raw.get("schedule") or {}
        storage = raw.get("storage") or {}
        return cls(
            version=int(raw.get("version", PREFERENCES_VERSION)),
            interests=dict(raw.get("interests") or {}),
            role_types=list(raw.get("role_types") or ["internship", "new_grad"]),
            full_time_searches=[
                str(q).strip()
                for q in ((raw.get("full_time") or {}).get("searches") or [])
                if str(q).strip()
            ],
            term_mode=terms.get("mode", "auto"),
            horizon_months=int(terms.get("horizon_months", 18)),
            explicit_terms=list(terms.get("explicit") or []),
            work_auth=WorkAuth(
                requires_sponsorship=bool(auth.get("requires_sponsorship", False)),
                citizen_only_ok=bool(auth.get("citizen_only_ok", True)),
            ),
            locations=list(locations.get("preferred") or []),
            exclude_countries=list(locations.get("exclude_countries") or []),
            skills=list(raw.get("skills") or []),
            highlights=list(raw.get("highlights") or []),
            notify_enabled=bool(notify.get("enabled", True)),
            notify_min_confidence=float(notify.get("min_confidence", 0.70)),
            notify_max_per_run=int(notify.get("max_per_run", 5)),
            schedule_enabled=bool(schedule.get("enabled", True)),
            interval_minutes=int(schedule.get("interval_minutes", 30)),
            company_interval_minutes=int(schedule.get("company_interval_minutes", 360)),
            store_scope=storage.get("store_scope", "taxonomy"),
            prune_inactive_after_days=int(storage.get("prune_inactive_after_days", 180)),
        )


DEFAULT_PREFERENCES = Preferences()


def load_prefs(path: Path | None = None) -> Preferences:
    """Read preferences, falling back to defaults when absent or unreadable.

    A corrupt file must not stop a scheduled run: the scout is still useful
    with defaults, and the user finds out through the UI rather than a crash.
    """
    if path is None:
        from roleradar.settings import settings

        path = settings.preferences_path
    try:
        return Preferences.from_dict(json.loads(Path(path).read_text()))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return Preferences()


def save_prefs(prefs: Preferences, path: Path | None = None) -> Path:
    if path is None:
        from roleradar.settings import settings

        path = settings.preferences_path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs.to_dict(), indent=2) + "\n")
    return path
