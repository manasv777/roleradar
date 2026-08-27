# roleradar

Scout internship and new-grad roles across job boards and company career sites,
filtered to the fields you actually care about.

roleradar polls community-maintained job feeds and employers' ATS boards on a
schedule, classifies what it finds, fetches the real job description, and tells
you what is worth your attention — with a desktop notification when something
new matches.

## Why it exists

Internship listings are scattered across a dozen GitHub repos and thousands of
company career pages. The good aggregators carry titles and links but **no job
descriptions**, so you cannot tell from the feed whether a role fits. roleradar
closes that gap: it fetches the description from the employer's ATS, scores it
against the skills you list, and surfaces the ones that match.

## What it does not do

roleradar does not write, tailor, or upload resumes, and it does not submit
applications for you. It finds roles and tells you about them. That is the whole
job.

**It makes no LLM calls.** No API key, no local model, no GPU. Classification,
fit scoring, and requirement extraction are all deterministic Python. This is
enforced by a test, not just a promise.

## Status

Early. The scout pipeline works; configuration, onboarding, and the web UI are
being built out. See `docs/` for the design.

## Requirements

- Python 3.11+
- Node 20+ (for the web UI)
- Chromium is **optional** — `pip install 'roleradar[render]'` plus
  `playwright install chromium` — and only improves coverage of direct company
  career sites. Everything else works without it.

## Install

```bash
git clone <repo-url> && cd roleradar
make setup
```

## License

Apache-2.0. roleradar derives its job-source fetching, normalization, and ATS
enrichment from [Resume-Matcher](https://github.com/srbhr/Resume-Matcher); see
`NOTICE` for attribution and `LICENSING.md` for how upstream job feeds are
treated.
