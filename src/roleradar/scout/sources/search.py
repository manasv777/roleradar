"""Full-time job search sources, built from the user's preferences.

The GitHub lists roleradar started with are internship and new-grad lists; they
carry essentially no full-time roles. These sources query general job boards
with the user's own search terms instead.

Nothing here is a fixed list of queries. The searches come from
`prefs.full_time_searches`, and when that is empty they are derived from the
specialties the user picked - so turning on full-time roles for "Data
Engineering" searches for data engineering without anyone typing it.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from roleradar.prefs import Preferences
from roleradar.scout.normalize import normalize_himalayas, normalize_jobicy
from roleradar.scout.sources.aggregators import SourceSpec
from roleradar.taxonomy import Taxonomy

__all__ = ["search_queries", "search_specs"]

HIMALAYAS_NOTE = (
    "Himalayas job search API (remote roles). Results are paginated about 20 at a "
    "time; roleradar reads up to 5 pages per search."
)
JOBICY_NOTE = (
    "Jobicy remote jobs API. Returns at most the 50 most recent jobs per search "
    "and does not paginate. Their terms ask that Jobicy is credited with a link "
    "and that apply links go to the job URL the feed provides - both are kept."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def search_queries(prefs: Preferences, taxonomy: Taxonomy) -> list[str]:
    """The searches to run: explicit ones, else the labels of chosen fields."""
    if prefs.full_time_searches:
        return list(dict.fromkeys(prefs.full_time_searches))

    derived: list[str] = []
    for key in prefs.selected_specialties():
        domain_id, _, specialty_id = key.partition("/")
        domain = taxonomy.domain(domain_id)
        if domain is None:
            continue
        if specialty_id == "*":
            derived.append(domain.label)
            continue
        for specialty in domain.specialties:
            if specialty.id == specialty_id:
                derived.append(specialty.label)
    return list(dict.fromkeys(derived))


def search_specs(prefs: Preferences, taxonomy: Taxonomy) -> list[SourceSpec]:
    """One Himalayas and one Jobicy source per search, or none if not wanted."""
    if "full_time" not in prefs.role_types:
        return []

    specs: list[SourceSpec] = []
    for query in search_queries(prefs, taxonomy):
        slug = _slug(query)
        specs.append(
            SourceSpec(
                source_id=f"himalayas:{slug}",
                url=f"https://himalayas.app/jobs/api/search?q={quote(query)}",
                normalizer=normalize_himalayas,
                root_key="jobs",
                note=HIMALAYAS_NOTE,
                page_param="page",
                page_style="page",
                max_pages=5,
            )
        )
        specs.append(
            SourceSpec(
                source_id=f"jobicy:{slug}",
                url=f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={quote(query)}",
                normalizer=normalize_jobicy,
                root_key="jobs",
                note=JOBICY_NOTE,
            )
        )
    return specs
