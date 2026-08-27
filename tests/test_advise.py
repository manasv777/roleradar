"""Tests for per-listing application advice.

Bad advice is worse than none: a false "you meet this requirement" sends the
candidate into an application he cannot win, and a false "missing" wastes a day
learning something he already knows. Every case below is one the matcher got
wrong against real postings before it was hardened.
"""

import pytest

from roleradar.scout.advise import (
    advise,
    compute_benchmarks,
    is_scoreable,
    requirement_met,
    requirement_terms,
)

pytestmark = pytest.mark.unit

PROFILE = {
    "summary": "Engineer who will go to any length. Current work is research.",
    "workExperience": [
        {"company": "UNC", "title": "Researcher",
         "description": ["Built Python data pipelines", "Trained a PyTorch model"]}
    ],
    "personalProjects": [
        {"name": "TextScope", "description": ["NLP pipeline with TF-IDF and scikit-learn"]}
    ],
    "additional": {"technicalSkills": ["Python", "PyTorch", "Java", "Git", "SQL"]},
}


class TestAlternatives:
    """Postings state requirements as alternatives far more often than singly."""

    def test_slash_alternatives_split(self) -> None:
        assert requirement_terms("TensorFlow/PyTorch/MXNet") == ["TensorFlow", "PyTorch", "MXNet"]

    def test_a_parenthetical_list_splits(self) -> None:
        assert requirement_terms("one language (Go, Java, Python)") == ["Go", "Java", "Python"]

    def test_a_single_parenthetical_offers_both_forms(self) -> None:
        """'version control (Git)' reported Git missing from a profile listing Git."""
        assert requirement_terms("version control (Git)") == ["Git", "version control"]

    def test_a_genuine_name_containing_a_slash_is_not_split(self) -> None:
        assert requirement_terms("CI/CD") == ["CI/CD"]

    def test_holding_any_alternative_satisfies_the_requirement(self) -> None:
        assert requirement_met("TensorFlow/PyTorch/MXNet", "", PROFILE["additional"]["technicalSkills"])


class TestFalsePositives:
    """The failure that matters most: claiming a fit the candidate does not have."""

    def test_two_letter_names_are_not_matched_against_prose(self) -> None:
        """'Go' matched the word 'go' in a sentence and reported a language he
        does not know. Short names are credited only from the skills list."""
        assert not requirement_met("Go", PROFILE["summary"], PROFILE["additional"]["technicalSkills"])

    def test_a_generic_qualifier_does_not_become_an_alternative(self) -> None:
        """'Current/Former HNTB Intern' split to ['Current', ...], and 'current'
        appears in ordinary prose — producing a 100% fit for a job requiring
        that he already interned there."""
        assert not is_scoreable("Current/Former HNTB Intern")

    @pytest.mark.parametrize("req", [
        "Excellent writing skills", "Detail-Oriented", "Computer Literacy",
        "Communication skills", "must be enrolled", "US citizenship required",
    ])
    def test_unscoreable_requirements_are_excluded(self, req: str) -> None:
        assert not is_scoreable(req)

    def test_real_skills_stay_scoreable(self) -> None:
        for req in ("Python", "PyTorch", "data structures and algorithms"):
            assert is_scoreable(req)


class TestRanking:
    def test_a_thin_posting_cannot_outrank_a_substantial_match(self) -> None:
        """A 1-of-1 'perfect fit' outranked a solid 5-of-8 until fit was
        weighted by how much the posting actually stated."""
        thin = advise({"listing_id": "a"}, PROFILE, {"required_skills": ["Python"]})
        rich = advise({"listing_id": "b"}, PROFILE,
                      {"required_skills": ["Python", "PyTorch", "Java", "Git", "SQL", "Rust"]})
        assert thin.fit_ratio > rich.fit_ratio, "thin posting does score higher on raw ratio"
        assert rich.priority > thin.priority, "but must not rank higher"

    def test_a_thin_posting_says_so_rather_than_claiming_a_match(self) -> None:
        a = advise({"listing_id": "a"}, PROFILE, {"required_skills": ["Python"]})
        assert "too little to judge" in a.verdict

    def test_a_deadline_dominates_ranking(self) -> None:
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        kw = {"required_skills": ["Python", "PyTorch", "Java"]}
        urgent = advise({"listing_id": "a", "deadline": soon}, PROFILE, kw)
        rolling = advise({"listing_id": "b"}, PROFILE, kw)
        assert urgent.priority > rolling.priority
        assert urgent.urgency == "closing"

    def test_benchmarks_come_from_the_corpus(self) -> None:
        """Fixed thresholds labelled ~every real posting 'weak' (median fit is
        0.24), which is true in the abstract and useless in practice."""
        median, p75 = compute_benchmarks([0.1, 0.2, 0.2, 0.3, 0.3, 0.4, 0.5, 0.9])
        assert median < p75
        assert 0.0 < median < 1.0

    def test_too_few_samples_falls_back_rather_than_inventing_a_benchmark(self) -> None:
        assert compute_benchmarks([0.5, 0.5]) == compute_benchmarks([])


class TestLogistics:
    def test_multi_location_postings_are_called_out_as_one_application(self) -> None:
        a = advise({"listing_id": "a"}, PROFILE, {}, duplicate_locations=21)
        assert any("ONE application" in x for x in a.logistics)

    def test_the_weakest_ats_gets_a_warning(self) -> None:
        a = advise({"listing_id": "a", "ats_vendor": "workday"}, PROFILE, {})
        assert any("Workday" in x for x in a.logistics)

    def test_an_inferred_work_auth_flag_is_never_presented_as_confirmed(self) -> None:
        a = advise({"listing_id": "a", "needs_verification": True}, PROFILE, {})
        assert any("INFERRED" in x for x in a.logistics)

    def test_no_deadline_is_stated_as_rolling_not_omitted(self) -> None:
        a = advise({"listing_id": "a"}, PROFILE, {})
        assert any("rolling" in x.lower() for x in a.logistics)


class TestEmphasis:
    def test_it_names_the_entries_that_actually_carry_the_skills(self) -> None:
        a = advise({"listing_id": "a"}, PROFILE,
                   {"required_skills": ["Python", "PyTorch", "SQL"]})
        joined = " ".join(a.emphasize)
        assert "UNC" in joined or "TextScope" in joined

    def test_gaps_are_stated_plainly(self) -> None:
        a = advise({"listing_id": "a"}, PROFILE,
                   {"required_skills": ["Python", "Rust", "Haskell", "Erlang"]})
        assert any("Gap to address" in e for e in a.emphasize)
