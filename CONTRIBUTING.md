# Contributing

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m pytest
```

The suite makes no network calls and no LLM calls. Anything reaching outward is
mocked with `respx` or marked `live` and excluded by default.

## Two rules that are not negotiable

**1. No LLM calls.** `tests/test_no_llm.py` parses every module for LLM client
imports and endpoint strings, and checks the declared dependencies. If it fails,
the fix is to solve the problem deterministically — the way `classify.py`,
`terms.py`, and `taxonomy.py` already do — not to add an exemption.

**2. Nothing hardcoded to one person's search.** No literal seasons, no assumed
work-authorization status, no fixed set of career fields. Terms come from
`terms.py`, fields from `config/taxonomy.yml`, everything else from
`data/preferences.json`. CI greps for `spring_20\d\d` outside test fixtures.

## Adding a source

Feeds named after a season get renamed or archived every year, which is why
`roleradar sources` exists. Run it before and after adding one.

1. Write a `normalize_<name>` in `scout/normalize.py` returning `ScoutRecord`.
2. Add a `SourceSpec` to `SOURCES` in `scout/sources/aggregators.py`, including
   a `note` recording the feed's licence.
3. Add a fixture and a test.

Be careful with sources that return **HTTP 200 with an empty result** for a bad
tenant slug — that failure looks exactly like "this company has no jobs", and it
looks that way forever.

## Adding a career field

Edit `config/taxonomy.yml`, then `roleradar reclassify`. No refetch needed.
Add the title to the taxonomy goldens if it is a case worth locking down.

## Style

Tests assert behaviour, not implementation. If you invert an existing
assertion, say so in the test — a test that quietly changes meaning is worse
than one that fails.
