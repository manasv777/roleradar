"""Job sources.

Two kinds, same contract: each produces `ScoutRecord`s and its own row in the
`sources` table so conditional-GET state is tracked per feed.

* `aggregators` — community-maintained lists (GitHub JSON/markdown feeds).
* `companies`   — a single employer's ATS board, fetched directly.
"""

from roleradar.scout.sources.aggregators import (
    SOURCES,
    SOURCES_BY_ID,
    FetchResult,
    SourceSpec,
    build_client,
    fetch_all,
    fetch_source,
)

__all__ = [
    "SOURCES",
    "SOURCES_BY_ID",
    "FetchResult",
    "SourceSpec",
    "build_client",
    "fetch_all",
    "fetch_source",
]
