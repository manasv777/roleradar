# roleradar

Scout internship and new-grad roles across job boards and company career sites,
filtered to the fields you actually care about.

roleradar polls community-maintained job feeds on a schedule, works out which
term and which career fields each posting belongs to, fetches the real job
description from the employer's applicant tracking system, and shows you what
is worth your attention.

**No API key. No local model. No GPU.** Classification, term resolution, and
requirement matching are all deterministic Python. This is enforced by a test
(`tests/test_no_llm.py`), not just promised in a README.

---

## Why it exists

Early-career listings are scattered across a dozen GitHub repos, each with its
own format. The good aggregators carry titles and links but **no job
descriptions**, so you cannot tell from the feed whether a role fits. And every
one of them is organised around somebody else's search — a fixed set of seasons,
a fixed idea of which roles count.

roleradar closes both gaps. It fetches the description from the employer's ATS,
and it lets you pick your own fields from a taxonomy you can edit.

## What it does not do

It does not write, tailor, or upload resumes, and it does not apply for you. It
finds roles and tells you about them.

---

## Requirements

- **Python 3.11+**
- **Node 20+** (for the web UI)
- Chromium is **optional**: `pip install -e '.[render]'` then
  `playwright install chromium`. It only improves coverage of direct company
  career pages; everything else works without it.

## Install

```bash
git clone https://github.com/manasv777/roleradar.git
cd roleradar

python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'

cd apps/web && npm install && cd ../..
```

## Set up your search

```bash
./.venv/bin/roleradar init
```

This asks which domains and specialties you want, whether you want internships,
new-grad roles or both, and whether you need visa sponsorship. It writes
`data/preferences.json`, which is plain JSON you can edit by hand afterwards.

Use `roleradar init --defaults` to skip the questions (software + AI/ML).

## Run it

Two processes, two terminals:

```bash
# API
./.venv/bin/python -m uvicorn roleradar.app:app --port 8100

# Web UI
cd apps/web && npm run dev
```

Then open **http://localhost:3100** and press **Run scout**. The first run takes
a few minutes and typically finds a few thousand listings.

Prefer the terminal?

```bash
./.venv/bin/roleradar run --once      # scout in this process
./.venv/bin/roleradar run --now       # ask a running server to scout
./.venv/bin/roleradar sources         # check every feed still answers
```

Once the API is running it scouts every 30 minutes on its own. No cron, no
launchd, no plist to edit.

---

## Picking your fields

`config/taxonomy.yml` defines 7 domains and 24 specialties — Software, Data,
AI/ML, Security, Hardware, Quant, Product. It is meant to be edited.

Matching is **multi-label**: an "ML Infrastructure Engineer Intern" is genuinely
both `ai_ml/ml_engineering` and `software/devops`, and forcing one label would
make the other wrong.

Add a specialty, then:

```bash
./.venv/bin/roleradar reclassify
```

Listings already on disk are re-judged against the new taxonomy **without
refetching anything**, because every row keeps the feed record it came from.
That is also why roleradar stores listings it cannot label rather than dropping
them: a discarded row cannot be reclassified later.

## Terms

Seasons are computed from the calendar, never hardcoded. `roleradar prefs` shows
which terms are currently in range. There is no year to update.

---

## Configuration

| File | What |
|---|---|
| `config/taxonomy.yml` | Career fields — edit freely, then `reclassify` |
| `data/preferences.json` | Your fields, terms, skills, schedule |

Environment overrides use a `ROLERADAR_` prefix (`ROLERADAR_PORT`,
`ROLERADAR_HOME`, `ROLERADAR_LOG_LEVEL`).

## Tests

```bash
./.venv/bin/python -m pytest        # ~290 tests, no network, no LLM
./.venv/bin/python -m pytest -m live  # opt-in: checks feeds are still alive
```

---

## Honesty notes

Two things this tool deliberately refuses to pretend about:

**Work authorization is inferred, never known.** No feed publishes visa
sponsorship reliably. If you tell roleradar you need sponsorship, it scores
listings as a *shortlist* and flags every one for verification. Confirm with the
employer before relying on it.

**Classification is a pile of regexes and will sometimes be wrong.** Every
listing carries a reason trace explaining why it was labelled as it was —
hover the confidence dot. If a decision looks wrong, you can see exactly which
rule produced it and fix the rule.

## Sources and their licensing

roleradar fetches public feeds at runtime and stores results in your local
database. It does not republish anyone's corpus. Each source's licence is
recorded in `config`/source notes and surfaced in the sources view;
`SimplifyJobs` in particular ships no licence, so treat its data as
all-rights-reserved and personal-use only. Company attribution (`company_url`)
is always preserved.

## Credits

Built on scouting code originally written for
[Resume-Matcher](https://github.com/srbhr/Resume-Matcher) (Apache-2.0). See
`NOTICE`.

## License

Apache-2.0.
