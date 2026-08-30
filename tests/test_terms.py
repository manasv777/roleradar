"""Terms must follow the calendar, not a constant.

The classifier this replaced hardcoded `spring_2027` / `summer_2027`. That is
correct for about a year and then the tool silently matches nothing - no error,
no empty-result warning, just a feed that quietly stops being useful. These
tests freeze the clock at several dates, including across a year boundary, so
that failure cannot reappear.
"""

from __future__ import annotations

from datetime import date

import pytest

from roleradar.terms import (
    Term,
    bare_season,
    canon_term,
    effective_term_count,
    horizon_terms,
    resolve_term,
    term_index,
)

pytestmark = pytest.mark.unit


class TestHorizon:
    @pytest.mark.parametrize(
        "today,expected_first",
        [
            (date(2026, 8, 29), "fall_2026"),
            (date(2026, 12, 1), "spring_2027"),
            (date(2027, 1, 15), "spring_2027"),   # January sits in the Winter/Spring cycle
            (date(2027, 7, 1), "fall_2027"),
            (date(2030, 3, 2), "spring_2030"),    # far future still resolves
        ],
    )
    def test_first_upcoming_term(self, today: date, expected_first: str) -> None:
        assert horizon_terms(today)[0].id == expected_first

    def test_horizon_is_ordered_and_bounded(self) -> None:
        terms = horizon_terms(date(2026, 8, 29), months=18)
        assert terms == sorted(terms)
        assert all(t.start >= date(2026, 8, 1) for t in terms)

    def test_winter_is_never_offered_separately(self) -> None:
        """Winter collapses into Spring, so offering both would name one cycle twice."""
        for today in (date(2026, 8, 29), date(2027, 1, 1), date(2028, 6, 30)):
            assert all(t.season != "winter" for t in horizon_terms(today))

    def test_a_longer_horizon_only_adds(self) -> None:
        short = horizon_terms(date(2026, 8, 29), months=6)
        long = horizon_terms(date(2026, 8, 29), months=24)
        assert set(t.id for t in short) <= set(t.id for t in long)


class TestParsing:
    def test_workday_underscore_is_matched(self) -> None:
        """`_` is a word character, so \\b would silently drop every Workday title."""
        assert canon_term("Software Engineering Co-op_Spring 2027").id == "spring_2027"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Summer 2027", "summer_2027"),
            ("summer '27", "summer_2027"),
            ("Fall 2026", "fall_2026"),
            ("Autumn 2026", "fall_2026"),     # alias
            ("Winter 2027", "spring_2027"),   # collapse
            ("SPRING-2027", "spring_2027"),
        ],
    )
    def test_spellings(self, text: str, expected: str) -> None:
        parsed = canon_term(text)
        assert parsed is not None and parsed.id == expected

    def test_no_term_returns_none(self) -> None:
        assert canon_term("Software Engineer Intern") is None

    def test_bare_season_is_recognised_separately(self) -> None:
        """vanshb03 emits a season with no year; the old parser dropped these."""
        assert bare_season("Summer") == "summer"
        assert bare_season("Autumn") == "fall"
        assert bare_season("Summer 2027") is None   # has a year, not bare


class TestCollapse:
    def test_winter_and_spring_are_one_cycle(self) -> None:
        pair = [Term("winter", 2027), Term("spring", 2027)]
        assert effective_term_count(pair) == 1

    def test_distinct_cycles_still_count(self) -> None:
        assert effective_term_count([Term("summer", 2027), Term("fall", 2027)]) == 2

    def test_label_names_both_halves(self) -> None:
        assert Term("spring", 2027).label == "Winter–Spring 2027"
        assert Term("summer", 2027).label == "Summer 2027"

    def test_index_accepts_every_spelling(self) -> None:
        index = term_index([Term("spring", 2027), Term("fall", 2026)])
        for spelling in ("spring 2027", "winter 2027", "fall 2026", "autumn 2026"):
            assert spelling in index


class TestResolve:
    HORIZON = [Term("fall", 2026), Term("spring", 2027), Term("summer", 2027)]

    def test_single_term_is_confident(self) -> None:
        term, confidence, _ = resolve_term(["Summer 2027"], horizon=self.HORIZON)
        assert term is not None and term.id == "summer_2027"
        assert confidence == 0.95

    def test_winter_spring_pair_is_not_demoted(self) -> None:
        """One January cycle written two ways must not read as a multi-term req."""
        _, confidence, _ = resolve_term(
            ["Winter 2027", "Spring 2027"], horizon=self.HORIZON
        )
        assert confidence == 0.95

    def test_a_few_terms_are_less_confident(self) -> None:
        _, confidence, _ = resolve_term(
            ["Spring 2027", "Summer 2027"], horizon=self.HORIZON
        )
        assert confidence == 0.70

    def test_evergreen_requisitions_are_demoted(self) -> None:
        _, confidence, reasons = resolve_term(
            ["Fall 2026", "Spring 2027", "Summer 2027", "Fall 2027"],
            horizon=self.HORIZON + [Term("fall", 2027)],
        )
        assert confidence == 0.30
        assert any("evergreen" in r for r in reasons)

    def test_title_recovery_is_lower_confidence(self) -> None:
        term, confidence, _ = resolve_term(
            [], title="SWE Intern, Summer 2027", horizon=self.HORIZON
        )
        assert term is not None and term.id == "summer_2027"
        assert confidence == 0.55

    def test_bare_season_assumes_the_next_matching_term(self) -> None:
        term, confidence, reasons = resolve_term(["Summer"], horizon=self.HORIZON)
        assert term is not None and term.id == "summer_2027"
        assert confidence == 0.40
        assert any("no year" in r for r in reasons)

    def test_out_of_horizon_terms_resolve_to_nothing(self) -> None:
        term, confidence, reasons = resolve_term(["Summer 2019"], horizon=self.HORIZON)
        assert term is None and confidence == 0.0
        assert any("outside the horizon" in r for r in reasons)

    def test_preference_breaks_a_tie(self) -> None:
        """With several valid terms, the one the user selected wins."""
        term, _, _ = resolve_term(
            ["Spring 2027", "Summer 2027"],
            horizon=self.HORIZON,
            preferred=["summer_2027"],
        )
        assert term is not None and term.id == "summer_2027"

    def test_every_outcome_carries_a_reason(self) -> None:
        for raw in ([], ["Summer 2027"], ["Summer 2019"], ["Summer"]):
            _, _, reasons = resolve_term(raw, horizon=self.HORIZON)
            assert reasons, f"{raw!r} produced no reason trace"
