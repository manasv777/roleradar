"""Classification: terms, role type, fields, and work authorization.

Several assertions here are the deliberate inverse of what the original suite
checked. The original dropped anything outside its author's fields and seasons,
so its tests asserted `bucket is None` as a *drop* signal. Labelling replaced
dropping, so those same inputs must now produce a stored, labelled row. Each
inversion is called out where it appears - a test that quietly flips meaning is
worse than one that fails.
"""

from __future__ import annotations

from datetime import date

import pytest

from roleradar.prefs import Preferences, WorkAuth
from roleradar.scout.classify import (
    ROLE_INTERNSHIP,
    ROLE_NEW_GRAD,
    ROLE_UNKNOWN,
    SPONSORSHIP_CITIZENSHIP,
    SPONSORSHIP_NONE,
    classify,
    classify_role_type,
    score_no_us_auth,
)
from roleradar.scout.normalize import ScoutRecord
from roleradar.taxonomy import load_taxonomy
from roleradar.terms import horizon_terms

pytestmark = pytest.mark.unit

TAXONOMY = load_taxonomy()
TODAY = date(2026, 8, 29)
HORIZON = horizon_terms(TODAY)


def rec(
    title: str = "Software Engineer Intern",
    *,
    terms: list[str] | None = None,
    category: str | None = "software",
    locations: list[str] | None = None,
    sponsorship: str | None = None,
    is_remote: bool = False,
    source_id: str = "test_feed",
) -> ScoutRecord:
    return ScoutRecord(
        source_id=source_id,
        external_id="ext-1",
        company="Acme",
        title=title,
        apply_url="https://example.com/job/1",
        company_url=None,
        locations=locations if locations is not None else ["New York, NY"],
        is_remote=is_remote,
        terms=terms if terms is not None else ["Summer 2027"],
        category=category,
        sponsorship=sponsorship,
        degrees=[],
        date_posted=None,
        date_updated=None,
        active=True,
        raw={},
    )


def judge(record: ScoutRecord, prefs: Preferences | None = None):
    return classify(
        record,
        prefs=prefs or Preferences(),
        taxonomy=TAXONOMY,
        horizon=HORIZON,
    )


class TestRoleType:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Software Engineer Intern", ROLE_INTERNSHIP),
            ("Engineering Co-op", ROLE_INTERNSHIP),
            ("Summer Analyst, Technology", ROLE_INTERNSHIP),
            ("New Grad Software Engineer", ROLE_NEW_GRAD),
            ("Software Engineer, University Graduate", ROLE_NEW_GRAD),
            ("Entry-Level Backend Developer", ROLE_NEW_GRAD),
            ("Senior Staff Engineer", ROLE_UNKNOWN),
            ("Principal Architect", ROLE_UNKNOWN),
        ],
    )
    def test_titles(self, title: str, expected: str) -> None:
        assert classify_role_type(title)[0] == expected

    def test_a_declared_source_type_wins(self) -> None:
        """New-grad feeds know what they are; do not re-derive it from a title."""
        role, reasons = classify_role_type(
            "Software Engineer", source_role_type=ROLE_NEW_GRAD
        )
        assert role == ROLE_NEW_GRAD
        assert any("source declares" in r for r in reasons)

    def test_a_term_implies_an_internship(self) -> None:
        assert classify_role_type("Software Engineer", has_term=True)[0] == ROLE_INTERNSHIP


class TestNewGradIsNoLongerDiscarded:
    """INVERSION. New-grad rows carry no `terms`, so the original returned
    `bucket=None` for every one and the store threw them all away."""

    def test_a_new_grad_row_classifies_without_a_term(self) -> None:
        verdict = judge(rec("New Grad Software Engineer", terms=[]))
        assert verdict.term is None            # genuinely has no academic term
        assert verdict.role_type == ROLE_NEW_GRAD
        assert verdict.domains == ["software"]  # and is still labelled


class TestFieldLabelling:
    def test_multi_label_is_preserved(self) -> None:
        """Forcing one label onto this title would make the other one a lie."""
        verdict = judge(rec("ML Infrastructure Engineer Intern", category=None))
        assert "software/devops" in verdict.specialties
        assert "ai_ml/ml_engineering" in verdict.specialties
        assert set(verdict.domains) == {"software", "ai_ml"}

    def test_data_analyst_is_analytics_not_ml(self) -> None:
        verdict = judge(rec("Data Analyst Intern", category=None))
        assert "data/analytics" in verdict.specialties
        assert not any(s.startswith("ai_ml/") for s in verdict.specialties)

    def test_non_engineering_titles_are_excluded(self) -> None:
        assert judge(rec("Sales Engineer Intern")).excluded is True
        assert judge(rec("Technical Recruiter Intern")).excluded is True

    def test_workday_underscore_title_still_resolves(self) -> None:
        verdict = judge(rec("Software Engineering Co-op_Spring 2027", terms=[]))
        assert verdict.term is not None and verdict.term.id == "spring_2027"

    def test_an_unmatched_title_is_labelled_not_dropped(self) -> None:
        """INVERSION: the original returned bucket=None here and the row died."""
        verdict = judge(rec("Underwater Basket Weaver", category=None, terms=[]))
        assert verdict.domains == []
        assert verdict.excluded is False          # not excluded, merely unlabelled
        assert verdict.reasons                    # and it still explains itself


class TestWorkAuthIsAPreference:
    def test_scoring_is_skipped_when_sponsorship_is_not_needed(self, monkeypatch) -> None:
        """The common case must cost nothing and claim nothing."""
        import roleradar.scout.classify as mod

        def explode(*_a, **_k):
            raise AssertionError("work-auth scoring ran for a user who does not need it")

        monkeypatch.setattr(mod, "score_no_us_auth", explode)
        verdict = judge(rec(), prefs=Preferences())
        assert verdict.work_auth_score is None
        assert verdict.needs_verification is False

    def test_scoring_runs_when_sponsorship_is_needed(self) -> None:
        prefs = Preferences(work_auth=WorkAuth(requires_sponsorship=True))
        verdict = judge(rec(locations=["London, United Kingdom"]), prefs)
        assert verdict.work_auth_score is not None
        assert verdict.needs_verification is True

    def test_the_classifier_never_claims_authority_on_visas(self) -> None:
        """No feed publishes work authorization, so this is a shortlist signal."""
        prefs = Preferences(work_auth=WorkAuth(requires_sponsorship=True))
        for locations in (["New York, NY"], ["Berlin, Germany"], []):
            assert judge(rec(locations=locations), prefs).needs_verification is True

    def test_citizenship_requirement_is_a_hard_veto(self) -> None:
        score, reasons = score_no_us_auth(rec(sponsorship=SPONSORSHIP_CITIZENSHIP))
        assert score == 0.0
        assert reasons

    def test_no_sponsorship_lowers_the_score(self) -> None:
        with_none = score_no_us_auth(rec(sponsorship=SPONSORSHIP_NONE))[0]
        without = score_no_us_auth(rec(sponsorship=None))[0]
        assert with_none < without or with_none == 0.0


class TestHonesty:
    """Invariants that must survive any taxonomy change."""

    def test_every_verdict_carries_a_reason_trace(self) -> None:
        for record in (
            rec(),
            rec("New Grad Engineer", terms=[]),
            rec("Sales Intern"),
            rec("Underwater Basket Weaver", category=None, terms=[]),
        ):
            assert judge(record).reasons, f"no reason trace for {record.title!r}"

    def test_confidence_stays_in_the_unit_interval(self) -> None:
        for record in (rec(), rec(terms=["Summer 2019"]), rec(terms=[])):
            assert 0.0 <= judge(record).confidence <= 1.0

    def test_classification_is_deterministic(self) -> None:
        record = rec("ML Engineer Intern")
        first, second = judge(record), judge(record)
        assert (first.term_id, first.role_type, first.specialties) == (
            second.term_id,
            second.role_type,
            second.specialties,
        )
