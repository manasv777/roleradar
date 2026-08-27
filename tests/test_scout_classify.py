"""Unit tests for Scout classification.

The highest-value test file in the feature. Pure functions, no network, no LLM.

The load-bearing assertion is ``test_no_auth_bucket_always_needs_verification``:
the work-authorization classifier is a heuristic over data that does not encode
the answer, and it must never present itself as authoritative. That invariant is
encoded here so it fails loudly if anyone relaxes it.
"""

import json
from pathlib import Path

import pytest

from roleradar.scout.classify import (
    BUCKET_FALL_2026_NO_AUTH,
    BUCKET_SPRING_2027,
    BUCKET_SUMMER_2027,
    TRACK_ML_AI,
    TRACK_OTHER,
    TRACK_SWE,
    bucket_terms,
    classify,
    classify_track,
    score_no_us_auth,
)
from roleradar.scout.normalize import ScoutRecord, normalize_simplify

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "scout" / "simplify_listings.json"


@pytest.fixture(scope="module")
def fixture_records() -> list[ScoutRecord]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [r for item in raw if (r := normalize_simplify(item, "simplify_intern"))]


def make(**kwargs: object) -> ScoutRecord:
    """Build a ScoutRecord with sensible defaults for the fields under test."""
    base: dict = {
        "source_id": "simplify_intern",
        "external_id": "x1",
        "company": "Acme",
        "title": "Software Engineer Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "category": "Software",
    }
    base.update(kwargs)
    return ScoutRecord(**base)  # type: ignore[arg-type]


class TestBucketTerms:
    def test_spring_2027_single_term_is_high_confidence(self) -> None:
        bucket, confidence, _ = bucket_terms(["Spring 2027"])
        assert bucket == BUCKET_SPRING_2027 and confidence == 0.95

    def test_winter_2027_maps_to_the_spring_bucket(self) -> None:
        """January starts are labelled inconsistently; both mean the same cycle."""
        bucket, confidence, _ = bucket_terms(["Winter 2027"])
        assert bucket == BUCKET_SPRING_2027 and confidence == 0.95

    def test_winter_plus_spring_counts_as_one_effective_term(self) -> None:
        """A genuine single-cycle Jan req must not be demoted as multi-term."""
        bucket, confidence, _ = bucket_terms(["Winter 2027", "Spring 2027"])
        assert bucket == BUCKET_SPRING_2027
        assert confidence == 0.95, "equivalent seasons should collapse to one term"

    def test_summer_2027(self) -> None:
        bucket, confidence, _ = bucket_terms(["Summer 2027"])
        assert bucket == BUCKET_SUMMER_2027 and confidence == 0.95

    def test_two_to_three_terms_is_mid_confidence(self) -> None:
        _, confidence, _ = bucket_terms(["Spring 2027", "Summer 2027"])
        assert confidence == 0.70

    def test_evergreen_requisition_is_demoted(self) -> None:
        """Postings listing every cycle are pipeline reqs, not real openings."""
        terms = [
            "Winter 2026", "Spring 2026", "Summer 2026", "Fall 2026",
            "Winter 2027", "Spring 2027", "Summer 2027", "Fall 2027",
        ]
        _, confidence, reasons = bucket_terms(terms)
        assert confidence == 0.30
        assert any("evergreen" in r for r in reasons)

    def test_multi_bucket_prefers_an_authorized_term(self) -> None:
        """A role reachable in Spring 2027 must not be filed as no-work-auth."""
        bucket, _, reasons = bucket_terms(["Fall 2026", "Winter 2027"])
        assert bucket == BUCKET_SPRING_2027
        assert any("spans multiple" in r for r in reasons)

    def test_no_terms_yields_no_bucket(self) -> None:
        bucket, confidence, reasons = bucket_terms([])
        assert bucket is None and confidence == 0.0
        assert reasons == ["no term signal"]

    def test_term_recovered_from_title_is_lower_confidence(self) -> None:
        bucket, confidence, reasons = bucket_terms(
            [], title="Software Engineering Co-op Spring 2027"
        )
        assert bucket == BUCKET_SPRING_2027 and confidence == 0.55
        assert any("recovered from title" in r for r in reasons)

    def test_out_of_scope_terms_yield_no_bucket(self) -> None:
        bucket, _, _ = bucket_terms(["Summer 2025", "Fall 2025"])
        assert bucket is None

    def test_underscore_joined_title_form(self) -> None:
        """Workday writes titles like `Co-op_Spring 2027`."""
        bucket, _, _ = bucket_terms([], title="Software Engineering Co-op_Spring 2027")
        assert bucket == BUCKET_SPRING_2027


class TestClassifyTrack:
    @pytest.mark.parametrize(
        "category,expected",
        [
            ("Software", TRACK_SWE),
            ("AI/ML/Data", TRACK_ML_AI),
            ("Hardware", TRACK_OTHER),
            ("Product", TRACK_OTHER),
            ("Quant", TRACK_OTHER),
        ],
    )
    def test_category_mapping(self, category: str, expected: str) -> None:
        track, _ = classify_track(category, "Intern")
        assert track == expected

    def test_data_analyst_demoted_out_of_ml(self) -> None:
        track, _ = classify_track("AI/ML/Data", "Business Intelligence Analyst Intern")
        assert track == TRACK_OTHER

    def test_sales_role_demoted_out_of_swe(self) -> None:
        track, _ = classify_track("Software", "Sales Engineer Intern")
        assert track == TRACK_OTHER

    def test_ml_inferred_from_title_without_category(self) -> None:
        track, _ = classify_track(None, "Machine Learning Research Scientist Intern")
        assert track == TRACK_ML_AI

    def test_swe_inferred_from_title_without_category(self) -> None:
        track, _ = classify_track(None, "Backend Software Developer Intern")
        assert track == TRACK_SWE


class TestScoreNoUsAuth:
    def test_citizenship_requirement_is_a_hard_veto(self) -> None:
        """No stack of positive signals may rescue a citizenship-only role."""
        record = make(
            source_id="curated_programs",
            title="Open Source Fellowship",
            locations=["London, UK"],
            is_remote=True,
            sponsorship="U.S. Citizenship is Required",
        )
        score, reasons = score_no_us_auth(record)
        assert score == 0.0
        assert any("VETO" in r for r in reasons)

    def test_curated_registry_entry_clears_threshold(self) -> None:
        record = make(source_id="curated_programs", title="Outreachy Internship")
        score, _ = score_no_us_auth(record)
        assert score >= 0.55

    def test_us_location_is_penalized(self) -> None:
        record = make(locations=["Mountain View, CA"])
        score, reasons = score_no_us_auth(record)
        assert score == 0.0
        assert any("requires US work authorization" in r for r in reasons)

    def test_non_us_location_scores_positive(self) -> None:
        record = make(locations=["Toronto, ON, Canada"])
        score, _ = score_no_us_auth(record)
        assert score > 0.0

    def test_score_is_clamped_to_unit_interval(self) -> None:
        record = make(
            source_id="curated_programs",
            title="Research Fellowship Grant",
            locations=["Berlin, Germany"],
            is_remote=True,
            sponsorship="Offers Sponsorship",
        )
        score, _ = score_no_us_auth(record)
        assert 0.0 <= score <= 1.0


class TestClassify:
    def test_spring_2027_needs_no_verification_flag(self) -> None:
        """The user has authorization for this bucket, so no caveat applies."""
        verdict = classify(make(terms=["Spring 2027"]))
        assert verdict.bucket == BUCKET_SPRING_2027
        assert verdict.needs_verification is False

    def test_summer_2027(self) -> None:
        verdict = classify(make(terms=["Summer 2027"], category="AI/ML/Data"))
        assert verdict.bucket == BUCKET_SUMMER_2027 and verdict.track == TRACK_ML_AI

    def test_fall_2026_non_us_lands_in_no_auth_bucket(self) -> None:
        verdict = classify(make(terms=["Fall 2026"], locations=["Toronto, ON, Canada"]))
        assert verdict.bucket == BUCKET_FALL_2026_NO_AUTH

    def test_fall_2026_us_onsite_is_dropped(self) -> None:
        """A US role in Fall 2026 is unusable — it must not be surfaced."""
        verdict = classify(make(terms=["Fall 2026"], locations=["Austin, TX"]))
        assert verdict.bucket is None

    def test_out_of_scope_track_is_dropped(self) -> None:
        verdict = classify(make(terms=["Spring 2027"], category="Hardware"))
        assert verdict.bucket is None

    def test_no_term_signal_is_dropped(self) -> None:
        assert classify(make(terms=[])).bucket is None

    def test_no_auth_bucket_always_needs_verification(self) -> None:
        """THE honesty invariant.

        Every row in the work-authorization-sensitive bucket must carry the
        verification flag, whatever its confidence. The classifier reads data
        that does not encode work-authorization requirements, so it is never
        authoritative and must never be rendered as though it were.
        """
        candidates = [
            make(terms=["Fall 2026"], locations=["Toronto, ON, Canada"]),
            make(terms=["Fall 2026"], locations=["London, UK"], is_remote=True),
            make(
                source_id="curated_programs",
                terms=["Fall 2026"],
                title="Outreachy Open Source Fellowship",
                locations=[],
            ),
            make(
                terms=["Fall 2026"],
                locations=["Berlin, Germany"],
                sponsorship="Offers Sponsorship",
            ),
        ]
        matched = 0
        for record in candidates:
            verdict = classify(record)
            if verdict.bucket == BUCKET_FALL_2026_NO_AUTH:
                matched += 1
                assert verdict.needs_verification is True, verdict
        assert matched > 0, "fixture must exercise the no-auth bucket at least once"

    def test_every_verdict_carries_a_reason_trace(self) -> None:
        """A bucket decision the user cannot audit is not acceptable."""
        for record in [
            make(terms=["Spring 2027"]),
            make(terms=["Fall 2026"], locations=["Toronto, ON, Canada"]),
            make(terms=[]),
        ]:
            assert classify(record).reasons, "verdict must explain itself"


class TestAgainstRealFixture:
    def test_no_auth_invariant_holds_across_fixture(
        self, fixture_records: list[ScoutRecord]
    ) -> None:
        for record in fixture_records:
            verdict = classify(record)
            if verdict.bucket == BUCKET_FALL_2026_NO_AUTH:
                assert verdict.needs_verification is True, record.title

    def test_verification_flag_only_on_the_no_auth_bucket(
        self, fixture_records: list[ScoutRecord]
    ) -> None:
        for record in fixture_records:
            verdict = classify(record)
            if verdict.needs_verification:
                assert verdict.bucket == BUCKET_FALL_2026_NO_AUTH, record.title

    def test_confidence_always_in_unit_interval(
        self, fixture_records: list[ScoutRecord]
    ) -> None:
        for record in fixture_records:
            assert 0.0 <= classify(record).confidence <= 1.0

    def test_classification_is_deterministic(
        self, fixture_records: list[ScoutRecord]
    ) -> None:
        for record in fixture_records:
            first, second = classify(record), classify(record)
            assert (first.bucket, first.confidence) == (second.bucket, second.confidence)


class TestFeedCategorySpellings:
    """Every category spelling in use must map explicitly.

    An unmapped value falls through to title guessing, which silently
    misclassifies whole feeds. These are the live values as of 2026-08-16.
    """

    @pytest.mark.parametrize(
        "category,expected",
        [
            ("Software", TRACK_SWE),                       # Simplify
            ("AI/ML/Data", TRACK_ML_AI),                    # Simplify
            ("Software Engineering", TRACK_SWE),            # Simplify legacy
            ("Data Science, AI & Machine Learning", TRACK_ML_AI),
            ("Data & ML/AI", TRACK_ML_AI),                  # zshah101
            ("Security", TRACK_SWE),                        # zshah101
            ("Quant", TRACK_OTHER),
            ("Hardware", TRACK_OTHER),
            ("Product", TRACK_OTHER),
        ],
    )
    def test_known_feed_categories_map_without_title_guessing(
        self, category: str, expected: str
    ) -> None:
        track, reasons = classify_track(category, "Intern")
        assert track == expected
        assert any("category" in r for r in reasons), (
            f"{category!r} fell through to title inference"
        )
