"""One place that knows "JS" and "JavaScript" are the same skill.

Every keyword check in this codebase was strictly literal — `_keyword_in_text`,
`_covered_by`, `_extract_jd_skill_keys` all do word-boundary regex on the exact
string the job description used. So a resume saying "JavaScript" scored zero
against a JD asking for "JS", and one saying "ML pipelines" scored zero against
"machine learning".

That cost twice over:

* **The score lied.** `keyword_match` is 55% of the ATS composite, so a resume
  that genuinely covered a requirement was marked as missing it.
* **The fix was blocked.** `refiner` classifies a keyword as *injectable* only
  when it can find it in the master profile. Under literal matching a keyword
  present under another name looked absent everywhere, so it was filed as
  non-injectable and nothing ever rephrased "ML" into "Machine Learning (ML)".

Matching is by **expansion**, not canonicalisation: a keyword is present when
*any* form in its group appears. That is deliberately symmetric, so it works
whichever side uses which spelling.

Scope is deliberately narrow — genuine synonyms and standard abbreviations for
one skill. Related-but-different technologies (React vs React Native, Java vs
JavaScript) are never grouped: inflating a match by pretending those are the
same would be a subtler version of the lie this module exists to fix.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Each row is a set of interchangeable names for ONE skill. Order is irrelevant;
# membership is what matters. Keep entries lowercase.
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    # Languages
    ("javascript", "js"),
    ("typescript", "ts"),
    ("python", "py"),
    ("go", "golang"),
    ("c++", "cpp", "cplusplus"),
    ("c#", "csharp"),
    ("objective-c", "objectivec"),
    ("ruby on rails", "rails", "ror"),
    # Web / frameworks
    ("react", "react.js", "reactjs"),
    ("node", "node.js", "nodejs"),
    ("vue", "vue.js", "vuejs"),
    ("angular", "angular.js", "angularjs"),
    ("next.js", "nextjs"),
    ("express", "express.js", "expressjs"),
    ("tailwind", "tailwindcss", "tailwind css"),
    # Data / storage
    ("postgresql", "postgres", "psql"),
    ("mongodb", "mongo"),
    ("elasticsearch", "elastic search", "es"),
    ("microsoft sql server", "mssql", "sql server"),
    ("bigquery", "big query"),
    # Infra / platform
    ("kubernetes", "k8s"),
    ("docker", "containerization", "containerisation"),
    ("amazon web services", "aws"),
    ("google cloud platform", "gcp", "google cloud"),
    ("microsoft azure", "azure"),
    ("continuous integration", "ci"),
    ("continuous deployment", "continuous delivery", "cd"),
    ("ci/cd", "cicd", "ci cd"),
    ("infrastructure as code", "iac"),
    ("terraform", "hashicorp terraform"),
    # AI / ML
    ("machine learning", "ml"),
    ("artificial intelligence", "ai"),
    ("deep learning", "dl"),
    ("natural language processing", "nlp"),
    ("computer vision", "cv"),
    ("reinforcement learning", "rl"),
    ("large language model", "large language models", "llm", "llms"),
    ("retrieval augmented generation", "retrieval-augmented generation", "rag"),
    ("neural network", "neural networks", "nn"),
    ("convolutional neural network", "cnn"),
    ("recurrent neural network", "rnn"),
    ("pytorch", "torch"),
    ("tensorflow", "tf"),
    ("scikit-learn", "sklearn", "scikit learn"),
    ("pandas", "pd"),
    ("numpy", "np"),
    ("exploratory data analysis", "eda"),
    ("extract transform load", "etl"),
    # Practice / process
    ("object oriented programming", "object-oriented programming", "oop"),
    ("test driven development", "test-driven development", "tdd"),
    ("application programming interface", "api", "apis"),
    ("representational state transfer", "rest", "restful"),
    ("graphql", "graph ql"),
    ("user interface", "ui"),
    ("user experience", "ux"),
    ("software development life cycle", "sdlc"),
    ("quality assurance", "qa"),
    ("agile", "scrum"),
    ("version control", "git"),
    ("data structures and algorithms", "data structures & algorithms", "dsa"),
    ("operating system", "operating systems", "os"),
    ("distributed systems", "distributed computing"),
    ("command line interface", "cli"),
    ("software as a service", "saas"),
    ("object relational mapping", "orm"),
)


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, frozenset[str]]:
    """name -> every interchangeable form of that skill (including itself)."""
    index: dict[str, set[str]] = {}
    for group in _ALIAS_GROUPS:
        members = set(group)
        for name in group:
            # A name appearing in two groups accumulates both, which is the
            # right behaviour for genuinely ambiguous abbreviations.
            index.setdefault(name, set()).update(members)
    return {k: frozenset(v) for k, v in index.items()}


def normalize(term: str) -> str:
    return re.sub(r"\s+", " ", (term or "").strip().lower())


def expand(term: str) -> frozenset[str]:
    """Every form of ``term``, or just ``term`` when it has no known aliases."""
    key = normalize(term)
    if not key:
        return frozenset()
    return _alias_index().get(key) or frozenset({key})


@lru_cache(maxsize=8192)
def _boundary_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher tolerant of the punctuation in real skill names.

    ``\\b`` is useless next to ``+`` or ``#`` (``c++``, ``c#``) because those
    are not word characters — the boundary lands in the wrong place and the
    term never matches. Lookarounds on word characters behave correctly for
    both plain and punctuated names.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def keyword_present(keyword: str, text: str) -> bool:
    """True when ``keyword`` — under any of its names — appears in ``text``."""
    if not keyword or not text:
        return False
    haystack = text.lower()
    return any(_boundary_pattern(form).search(haystack) for form in expand(keyword))


def matching_forms(keyword: str, text: str) -> list[str]:
    """Which alias forms actually occur — for explaining a match."""
    if not keyword or not text:
        return []
    haystack = text.lower()
    return sorted(f for f in expand(keyword) if _boundary_pattern(f).search(haystack))
