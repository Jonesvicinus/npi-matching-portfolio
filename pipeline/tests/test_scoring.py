import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scoring import (
    professional_composite, center_composite,
    zip_compare, assess_candidate_strength, assess_selection_confidence,
)


class TestProfessionalComposite:
    def test_all_signals_present(self):
        score, signals, _ = professional_composite(1.0, 1.0, True, True, city_missing=False)
        assert score == pytest.approx(1.0)
        assert "name" in signals
        assert "city" in signals
        assert "credential" in signals
        assert "taxonomy" in signals

    def test_name_and_city_only(self):
        score, signals, _ = professional_composite(1.0, 1.0, False, False, city_missing=False)
        assert score == pytest.approx(0.80)   # 1.0*0.55 + 1.0*0.25
        assert "city" in signals
        assert "credential" not in signals
        assert "taxonomy" not in signals

    def test_city_missing_redistributes_to_name(self):
        score, signals, _ = professional_composite(1.0, 0.0, False, False, city_missing=True)
        assert score == pytest.approx(0.80)   # 1.0*0.80
        assert "city_missing" in signals
        assert "city" not in signals

    def test_city_missing_all_other_signals_capped_at_ceiling(self):
        score, signals, _ = professional_composite(1.0, 0.0, True, True, city_missing=True)
        assert score == pytest.approx(0.92)   # 1.0*0.80+0.10+0.10=1.0 → cap 0.92
        assert "city_missing" in signals

    def test_city_missing_unique_pushes_past_ceiling(self):
        score, signals, _ = professional_composite(1.0, 0.0, False, False, city_missing=True, is_unique=True)
        assert score == pytest.approx(0.90)   # 0.80 + 0.10 bonus
        assert "unique" in signals
        assert "city_missing" in signals

    def test_city_missing_unique_with_other_signals(self):
        # 1.0*0.85+0.075+0.075=1.0 → cap 0.92, then +0.10 unique = 1.0
        score, signals, _ = professional_composite(1.0, 0.0, True, True, city_missing=True, is_unique=True)
        assert score == pytest.approx(1.0)

    def test_unique_normal_bonus_no_city_missing(self):
        score, signals, _ = professional_composite(0.90, 1.0, True, True, city_missing=False, is_unique=True)
        assert score == pytest.approx(0.995)  # 0.90*0.55+0.25+0.10+0.10=0.945 + 0.05 bonus
        assert "unique" in signals

    def test_city_val_zero_no_city_signal(self):
        score, signals, _ = professional_composite(1.0, 0.0, False, False, city_missing=False)
        assert score == pytest.approx(0.55)   # 1.0*0.55
        assert "city" not in signals

    def test_name_signal_at_85(self):
        score, signals, _ = professional_composite(0.85, 0.0, False, False, city_missing=False)
        assert "name" in signals
        assert "name_good" not in signals

    def test_name_good_signal_at_82(self):
        score, signals, _ = professional_composite(0.82, 0.0, False, False, city_missing=False)
        assert "name_good" in signals
        assert "name" not in signals

    def test_no_name_signal_below_80(self):
        score, signals, _ = professional_composite(0.70, 0.0, False, False, city_missing=False)
        assert "name" not in signals
        assert "name_good" not in signals


class TestCenterComposite:
    def test_all_signals(self):
        score, signals, _ = center_composite(1.0, True, True, zip_missing=False)
        assert score == pytest.approx(1.0)
        assert "name" in signals
        assert "city" in signals
        assert "zip" in signals

    def test_zip_missing_redistributes(self):
        score, signals, _ = center_composite(1.0, True, False, zip_missing=True)
        assert score == pytest.approx(1.0)
        assert "zip" not in signals
        assert "city" in signals

    def test_city_match_no_zip(self):
        score, signals, _ = center_composite(0.80, True, False, zip_missing=False)
        assert score == pytest.approx(0.68)
        assert "city" in signals
        assert "zip" not in signals

    def test_name_only_no_location(self):
        score, signals, _ = center_composite(0.90, False, False, zip_missing=True)
        assert score == pytest.approx(0.72)
        assert "city" not in signals
        assert "zip" not in signals

    def test_unique_bonus(self):
        score, signals, _ = center_composite(0.90, False, False, zip_missing=True, is_unique=True)
        assert score == pytest.approx(min(1.0, 0.90 * 0.80 + 0.05))
        assert "unique" in signals

    def test_name_signal_at_85(self):
        score, signals, _ = center_composite(0.85, False, False, zip_missing=True)
        assert "name" in signals

    def test_name_only_zip_present_no_matches(self):
        # name only, both city and zip present but neither matched
        score, signals, _ = center_composite(0.70, False, False, zip_missing=False)
        assert score == pytest.approx(0.42)   # 0.70×0.60
        assert "city" not in signals
        assert "zip" not in signals



class TestCenterScoringIntegration:
    def test_high_name_city_zip(self):
        score, signals, _ = center_composite(0.92, True, True, zip_missing=False)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "HIGH"

    def test_medium_decent_name_city_zip_missing(self):
        # 0.75*0.80 + 1.0*0.20 = 0.80 — was HIGH with old threshold, now MEDIUM
        score, signals, _ = center_composite(0.75, True, False, zip_missing=True)
        assert score == pytest.approx(0.80)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "MEDIUM"

    def test_low_weak_name_no_location(self):
        score, signals, _ = center_composite(0.55, False, False, zip_missing=True)
        assert score == pytest.approx(0.44)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "LOW"


class TestProfessionalPhase1Integration:
    def test_high_all_signals(self):
        score, signals, _ = professional_composite(0.90, 1.0, True, True, city_missing=False)
        assert score == pytest.approx(0.945)  # 0.90*0.55+0.25+0.10+0.10
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "HIGH"

    def test_medium_name_city_credential(self):
        score, signals, _ = professional_composite(0.82, 1.0, True, False, city_missing=False)
        assert score == pytest.approx(0.801)  # 0.82*0.55+0.25+0.10
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "MEDIUM"

    def test_low_name_only_no_city(self):
        score, signals, _ = professional_composite(0.70, 0.0, False, False, city_missing=False)
        assert score == pytest.approx(0.385)  # 0.70*0.55
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "LOW"

    def test_city_missing_perfect_name_needs_credential_for_high(self):
        # With name weight 0.80 in city_missing, perfect name alone = 0.80 < HIGH threshold
        score, signals, _ = professional_composite(1.0, 0.0, False, False, city_missing=True)
        assert score == pytest.approx(0.80)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "MEDIUM"

    def test_city_missing_perfect_name_with_credential_is_high(self):
        # 1.0*0.80 + 0.10 credential = 0.90 → HIGH
        score, signals, _ = professional_composite(1.0, 0.0, True, False, city_missing=True)
        assert score == pytest.approx(0.90)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "HIGH"

    def test_city_missing_unique_high(self):
        # 0.80 base + 0.10 unique bonus = 0.90 → HIGH
        score, signals, _ = professional_composite(1.0, 0.0, False, False, city_missing=True, is_unique=True)
        assert score == pytest.approx(0.90)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "HIGH"


class TestProfessionalPhase2AnchorLogic:
    def test_anchor_approved_city_always_1(self):
        score, signals, _ = professional_composite(0.90, 1.0, True, True, city_missing=False)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "HIGH"

    def test_anchor_inferred_city_must_match(self):
        score_no_city, _, _ = professional_composite(0.90, 0.0, True, True, city_missing=False)
        score_city, _, _    = professional_composite(0.90, 1.0, True, True, city_missing=False)
        assert score_city > score_no_city

    def test_anchor_inferred_city_mismatch_lower_confidence(self):
        score, signals, _ = professional_composite(0.90, 0.0, False, False, city_missing=False)
        tier, _ = assess_selection_confidence(score, False, None)
        assert tier == "LOW"


class TestProfessionalFieldScores:
    def test_all_signals_field_scores(self):
        _, _, fs = professional_composite(0.92, 1.0, True, True, city_missing=False)
        assert fs["name"] == pytest.approx(0.92)
        assert fs["city"] == pytest.approx(1.0)
        assert fs["credential"] == pytest.approx(1.0)
        assert fs["taxonomy"] == pytest.approx(1.0)

    def test_city_missing_field_scores_city_is_none(self):
        _, _, fs = professional_composite(0.90, 0.0, True, True, city_missing=True)
        assert fs["city"] is None

    def test_city_present_no_match_field_scores_city_is_zero(self):
        _, _, fs = professional_composite(0.90, 0.0, True, True, city_missing=False)
        assert fs["city"] == pytest.approx(0.0)

    def test_no_cred_no_tax_field_scores(self):
        _, _, fs = professional_composite(0.80, 1.0, False, False, city_missing=False)
        assert fs["credential"] == pytest.approx(0.0)
        assert fs["taxonomy"] == pytest.approx(0.0)

    def test_field_scores_keys_present(self):
        _, _, fs = professional_composite(0.85, 0.5, False, True, city_missing=False)
        assert set(fs.keys()) == {"name", "city", "credential", "taxonomy"}


class TestCenterFieldScores:
    def test_all_signals_field_scores(self):
        _, _, fs = center_composite(0.92, True, True, zip_missing=False)
        assert fs["name"] == pytest.approx(0.92)
        assert fs["city"] == pytest.approx(1.0)
        assert fs["zip"] == pytest.approx(1.0)

    def test_zip_missing_field_scores_zip_is_none(self):
        _, _, fs = center_composite(0.90, True, False, zip_missing=True)
        assert fs["zip"] is None

    def test_zip_present_no_match_field_scores_zip_is_zero(self):
        _, _, fs = center_composite(0.90, True, False, zip_missing=False)
        assert fs["zip"] == pytest.approx(0.0)

    def test_city_no_match_field_scores_city_is_zero(self):
        _, _, fs = center_composite(0.90, False, False, zip_missing=False)
        assert fs["city"] == pytest.approx(0.0)

    def test_city_missing_field_scores_city_is_none(self):
        _, _, fs = center_composite(0.90, False, False, zip_missing=False, city_missing=True)
        assert fs["city"] is None

    def test_field_scores_keys_present(self):
        _, _, fs = center_composite(0.85, True, True, zip_missing=False)
        assert set(fs.keys()) == {"name", "city", "zip"}

    def test_zip_match_level_zip9_signal(self):
        _, signals, _ = center_composite(0.90, True, True, zip_missing=False, zip_match_level="zip9")
        assert "zip9" in signals
        assert "zip" not in signals
        assert "zip5" not in signals

    def test_zip_match_level_zip5_signal(self):
        _, signals, _ = center_composite(0.90, True, True, zip_missing=False, zip_match_level="zip5")
        assert "zip5" in signals
        assert "zip" not in signals

    def test_zip_match_level_none_falls_back_to_zip(self):
        _, signals, _ = center_composite(0.90, True, True, zip_missing=False, zip_match_level=None)
        assert "zip" in signals


class TestZipCompare:
    def test_zip9_exact_match(self):
        assert zip_compare("10001-1234", "10001-1234") == (True, "zip9")

    def test_zip5_vs_zip9_fallback(self):
        assert zip_compare("10001", "10001-1234") == (True, "zip5")

    def test_zip9_vs_zip5_fallback(self):
        assert zip_compare("10001-1234", "10001") == (True, "zip5")

    def test_malformed_zip4_falls_back_to_zip5(self):
        # 10087-332 → 8 digits after stripping — not enough for zip9, falls back to zip5
        assert zip_compare("10087-332", "10087-1234") == (True, "zip5")

    def test_different_zip4_same_zip5(self):
        assert zip_compare("10001-1234", "10001-5678") == (True, "zip5")

    def test_zip5_mismatch(self):
        assert zip_compare("10001", "10002") == (False, "none")

    def test_none_input(self):
        assert zip_compare(None, "10001") == (False, "none")
        assert zip_compare("10001", None) == (False, "none")

    def test_too_few_digits(self):
        assert zip_compare("1234", "10001") == (False, "none")
        assert zip_compare("10001", "123") == (False, "none")

    def test_phone_number_rejected(self):
        # 314-439-0800 → 3144390800 → 10 digits → > 9 → rejected
        assert zip_compare("314-439-0800", "31443-1234") == (False, "none")

    def test_url_rejected(self):
        assert zip_compare("https://example.com/12345", "10001") == (False, "none")
        assert zip_compare("http://x.com", "10001") == (False, "none")
        assert zip_compare("www.hospital.org", "10001") == (False, "none")

    def test_trailing_spaces_handled(self):
        assert zip_compare("10001  ", "10001") == (True, "zip5")

    def test_both_valid_zip9_different(self):
        # Different ZIP+4, same ZIP5 → zip5 match
        assert zip_compare("90210-1234", "90210-9999") == (True, "zip5")

    def test_both_valid_zip9_fully_different(self):
        assert zip_compare("10001-1234", "90210-5678") == (False, "none")


class TestAssessCandidateStrength:
    def test_strong_at_boundary(self):
        assert assess_candidate_strength(0.85) == "STRONG"

    def test_strong_above_boundary(self):
        assert assess_candidate_strength(1.0) == "STRONG"
        assert assess_candidate_strength(0.92) == "STRONG"

    def test_moderate_just_below_strong(self):
        assert assess_candidate_strength(0.84) == "MODERATE"

    def test_moderate_at_boundary(self):
        assert assess_candidate_strength(0.65) == "MODERATE"

    def test_weak_just_below_moderate(self):
        assert assess_candidate_strength(0.64) == "WEAK"

    def test_weak_at_zero(self):
        assert assess_candidate_strength(0.0) == "WEAK"

    def test_uses_final_score_not_name_score(self):
        # candidate_strength is derived from final composite, not raw name similarity
        # a score of 0.84 is MODERATE even if name_score was 0.92
        assert assess_candidate_strength(0.84) == "MODERATE"


class TestAssessSelectionConfidence:
    # HIGH cases
    def test_high_no_conflict_no_margin(self):
        assert assess_selection_confidence(0.85, False, None, name_score=0.95) == ("HIGH", [])

    def test_high_single_candidate(self):
        assert assess_selection_confidence(0.92, False, None, name_score=0.95) == ("HIGH", [])

    def test_high_clear_margin(self):
        assert assess_selection_confidence(0.90, False, 0.12, name_score=0.95) == ("HIGH", [])

    def test_high_blocked_by_city_conflict(self):
        tier, flags = assess_selection_confidence(0.90, True, None)
        assert tier == "MEDIUM"
        assert "city_conflict" in flags

    def test_high_blocked_by_margin_too_close(self):
        tier, flags = assess_selection_confidence(0.90, False, 0.05, name_score=0.80)
        assert tier == "MEDIUM"
        assert "margin_too_close" in flags

    def test_high_blocked_by_both_flags(self):
        tier, flags = assess_selection_confidence(0.90, True, 0.03, name_score=0.80)
        assert tier == "MEDIUM"
        assert "city_conflict" in flags
        assert "margin_too_close" in flags

    def test_margin_exactly_at_boundary_passes(self):
        assert assess_selection_confidence(0.90, False, 0.08, name_score=0.95) == ("HIGH", [])

    def test_margin_just_below_boundary_blocks(self):
        tier, flags = assess_selection_confidence(0.90, False, 0.079, name_score=0.80)
        assert tier == "MEDIUM"
        assert "margin_too_close" in flags

    def test_near_perfect_name_exempt_from_margin_too_close(self):
        # name_score >= 0.95 → margin_too_close never fires → HIGH
        tier, flags = assess_selection_confidence(0.90, False, 0.05, name_score=0.97)
        assert tier == "HIGH"
        assert "margin_too_close" not in flags

    def test_name_below_threshold_blocks_high(self):
        tier, flags = assess_selection_confidence(0.90, False, None, name_score=0.88)
        assert tier == "MEDIUM"
        assert "name_below_threshold" in flags

    # MEDIUM cases
    def test_medium_no_flags(self):
        tier, flags = assess_selection_confidence(0.75, False, None, name_score=0.95)
        assert tier == "MEDIUM"
        assert flags == []

    def test_medium_with_city_conflict_shows_flag(self):
        # Option B: city_conflict shown even on MEDIUM
        tier, flags = assess_selection_confidence(0.75, True, None)
        assert tier == "MEDIUM"
        assert "city_conflict" in flags

    def test_medium_with_margin_too_close_shows_flag(self):
        tier, flags = assess_selection_confidence(0.70, False, 0.03, name_score=0.80)
        assert tier == "MEDIUM"
        assert "margin_too_close" in flags

    def test_medium_at_boundary(self):
        assert assess_selection_confidence(0.65, False, None)[0] == "MEDIUM"

    # LOW cases
    def test_low_score(self):
        tier, flags = assess_selection_confidence(0.50, False, None, name_score=0.95)
        assert tier == "LOW"
        assert flags == []

    def test_low_with_city_conflict_shows_flag(self):
        # Option B: city_conflict shown on LOW too
        tier, flags = assess_selection_confidence(0.50, True, None)
        assert tier == "LOW"
        assert "city_conflict" in flags

    def test_low_margin_not_flagged(self):
        # match_score < MEDIUM_THRESHOLD → margin_too_close guard prevents flag
        tier, flags = assess_selection_confidence(0.50, False, 0.03)
        assert tier == "LOW"
        assert "margin_too_close" not in flags

    def test_low_at_boundary(self):
        assert assess_selection_confidence(0.64, False, None)[0] == "LOW"

    # perfect_name informational flag
    def test_perfect_name_flag_on_high(self):
        tier, flags = assess_selection_confidence(0.92, False, None, name_score=1.0)
        assert tier == "HIGH"
        assert "perfect_name" in flags

    def test_perfect_name_flag_on_medium(self):
        tier, flags = assess_selection_confidence(0.75, False, None, name_score=1.0)
        assert tier == "MEDIUM"
        assert "perfect_name" in flags

    def test_perfect_name_flag_on_low(self):
        tier, flags = assess_selection_confidence(0.50, False, None, name_score=1.0)
        assert tier == "LOW"
        assert "perfect_name" in flags

    def test_near_perfect_name_no_flag(self):
        tier, flags = assess_selection_confidence(0.92, False, None, name_score=0.99)
        assert tier == "HIGH"
        assert "perfect_name" not in flags

    def test_perfect_name_with_city_conflict_still_flags_both(self):
        tier, flags = assess_selection_confidence(0.90, True, None, name_score=1.0)
        assert tier == "MEDIUM"
        assert "city_conflict" in flags
        assert "perfect_name" in flags

    # Ambiguous set: margin_too_close applies to all candidates
    def test_margin_too_close_both_candidates_get_medium(self):
        # rank-1: 0.91, rank-2: 0.89, margin=0.02, moderate name score
        r1_tier, r1_flags = assess_selection_confidence(0.91, False, 0.02, name_score=0.80)
        r2_tier, r2_flags = assess_selection_confidence(0.89, False, 0.02, name_score=0.80)
        assert r1_tier == "MEDIUM"
        assert r2_tier == "MEDIUM"
        assert "margin_too_close" in r1_flags
        assert "margin_too_close" in r2_flags


class TestSiblingConstants:
    def test_sibling_constants_exist_with_expected_values(self):
        import match_medical_centers as mmc
        assert mmc.SIBLING_NAME_THRESHOLD == 0.97
        assert mmc.SIBLING_FLAG_THRESHOLD == 0.90
