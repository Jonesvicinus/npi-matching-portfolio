import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scoring import professional_composite, center_composite, confidence_from_score


class TestProfessionalComposite:
    def test_all_signals_present(self):
        score, signals = professional_composite(1.0, 1.0, True, True, city_missing=False)
        assert score == pytest.approx(1.0)
        assert "name" in signals
        assert "city" in signals
        assert "credential" in signals
        assert "taxonomy" in signals

    def test_name_and_city_only(self):
        score, signals = professional_composite(1.0, 1.0, False, False, city_missing=False)
        assert score == pytest.approx(0.70)
        assert "city" in signals
        assert "credential" not in signals
        assert "taxonomy" not in signals

    def test_city_missing_redistributes_to_name(self):
        score, signals = professional_composite(1.0, 0.0, False, False, city_missing=True)
        assert score == pytest.approx(0.70)
        assert "city_missing" in signals
        assert "city" not in signals

    def test_city_missing_all_other_signals_capped_at_ceiling(self):
        score, signals = professional_composite(1.0, 0.0, True, True, city_missing=True)
        assert score == pytest.approx(0.75)
        assert "city_missing" in signals

    def test_city_missing_unique_pushes_past_ceiling(self):
        score, signals = professional_composite(1.0, 0.0, False, False, city_missing=True, is_unique=True)
        assert score == pytest.approx(0.80)
        assert "unique" in signals
        assert "city_missing" in signals

    def test_city_missing_unique_with_other_signals(self):
        # ceiling applied first: min(1.0, 0.75) = 0.75, then +0.10 unique bonus = 0.85
        score, signals = professional_composite(1.0, 0.0, True, True, city_missing=True, is_unique=True)
        assert score == pytest.approx(0.85)

    def test_unique_normal_bonus_no_city_missing(self):
        score, signals = professional_composite(0.90, 1.0, True, True, city_missing=False, is_unique=True)
        assert score == pytest.approx(1.0)
        assert "unique" in signals

    def test_city_val_zero_no_city_signal(self):
        score, signals = professional_composite(1.0, 0.0, False, False, city_missing=False)
        assert score == pytest.approx(0.50)
        assert "city" not in signals

    def test_name_signal_at_85(self):
        score, signals = professional_composite(0.85, 0.0, False, False, city_missing=False)
        assert "name" in signals
        assert "name_good" not in signals

    def test_name_good_signal_at_82(self):
        score, signals = professional_composite(0.82, 0.0, False, False, city_missing=False)
        assert "name_good" in signals
        assert "name" not in signals

    def test_no_name_signal_below_80(self):
        score, signals = professional_composite(0.70, 0.0, False, False, city_missing=False)
        assert "name" not in signals
        assert "name_good" not in signals


class TestCenterComposite:
    def test_all_signals(self):
        score, signals = center_composite(1.0, True, True, zip_missing=False)
        assert score == pytest.approx(1.0)
        assert "name" in signals
        assert "city" in signals
        assert "zip" in signals

    def test_zip_missing_redistributes(self):
        score, signals = center_composite(1.0, True, False, zip_missing=True)
        assert score == pytest.approx(1.0)
        assert "zip" not in signals
        assert "city" in signals

    def test_city_match_no_zip(self):
        score, signals = center_composite(0.80, True, False, zip_missing=False)
        assert score == pytest.approx(0.68)
        assert "city" in signals
        assert "zip" not in signals

    def test_name_only_no_location(self):
        score, signals = center_composite(0.90, False, False, zip_missing=True)
        assert score == pytest.approx(0.72)
        assert "city" not in signals
        assert "zip" not in signals

    def test_unique_bonus(self):
        score, signals = center_composite(0.90, False, False, zip_missing=True, is_unique=True)
        assert score == pytest.approx(min(1.0, 0.90 * 0.80 + 0.05))
        assert "unique" in signals

    def test_name_signal_at_85(self):
        score, signals = center_composite(0.85, False, False, zip_missing=True)
        assert "name" in signals

    def test_name_only_zip_present_no_matches(self):
        # name only, both city and zip present but neither matched
        score, signals = center_composite(0.70, False, False, zip_missing=False)
        assert score == pytest.approx(0.42)   # 0.70×0.60
        assert "city" not in signals
        assert "zip" not in signals


class TestConfidenceFromScore:
    def test_high_at_boundary(self):
        assert confidence_from_score(0.80) == "HIGH"

    def test_high_above_boundary(self):
        assert confidence_from_score(1.0) == "HIGH"
        assert confidence_from_score(0.95) == "HIGH"

    def test_medium_just_below_high(self):
        assert confidence_from_score(0.79) == "MEDIUM"

    def test_medium_at_boundary(self):
        assert confidence_from_score(0.60) == "MEDIUM"

    def test_low_just_below_medium(self):
        assert confidence_from_score(0.59) == "LOW"

    def test_low_at_zero(self):
        assert confidence_from_score(0.0) == "LOW"


class TestCenterScoringIntegration:
    def test_high_name_city_zip(self):
        score, signals = center_composite(0.92, True, True, zip_missing=False)
        assert confidence_from_score(score) == "HIGH"

    def test_high_decent_name_city_zip_missing(self):
        score, signals = center_composite(0.75, True, False, zip_missing=True)
        assert score == pytest.approx(0.80)   # 0.75×0.80+0.20
        assert confidence_from_score(score) == "HIGH"

    def test_low_weak_name_no_location(self):
        score, signals = center_composite(0.55, False, False, zip_missing=True)
        assert score == pytest.approx(0.44)   # 0.55×0.80
        assert confidence_from_score(score) == "LOW"


class TestProfessionalPhase1Integration:
    def test_high_all_signals(self):
        score, signals = professional_composite(0.90, 1.0, True, True, city_missing=False)
        assert score == pytest.approx(0.95)
        assert confidence_from_score(score) == "HIGH"

    def test_medium_name_city_credential(self):
        score, signals = professional_composite(0.82, 1.0, True, False, city_missing=False)
        assert score == pytest.approx(0.76)
        assert confidence_from_score(score) == "MEDIUM"

    def test_low_name_only_no_city(self):
        score, signals = professional_composite(0.70, 0.0, False, False, city_missing=False)
        assert score == pytest.approx(0.35)
        assert confidence_from_score(score) == "LOW"

    def test_city_missing_name_only_capped(self):
        score, signals = professional_composite(1.0, 0.0, False, False, city_missing=True)
        assert score == pytest.approx(0.70)
        assert confidence_from_score(score) == "MEDIUM"

    def test_city_missing_unique_reaches_high(self):
        score, signals = professional_composite(1.0, 0.0, False, False, city_missing=True, is_unique=True)
        assert score == pytest.approx(0.80)
        assert confidence_from_score(score) == "HIGH"


class TestProfessionalPhase2AnchorLogic:
    def test_anchor_approved_city_always_1(self):
        # anchor_approved → city_val=1.0 regardless of actual city match
        score, signals = professional_composite(0.90, 1.0, True, True, city_missing=False)
        assert confidence_from_score(score) == "HIGH"

    def test_anchor_inferred_city_must_match(self):
        # anchor_inferred → city_val=0.0 if cities don't match
        score_no_city, _ = professional_composite(0.90, 0.0, True, True, city_missing=False)
        score_city, _    = professional_composite(0.90, 1.0, True, True, city_missing=False)
        assert score_city > score_no_city

    def test_anchor_inferred_city_mismatch_lower_confidence(self):
        # Strong name but inferred anchor and city mismatch → LOW
        score, signals = professional_composite(0.90, 0.0, False, False, city_missing=False)
        assert confidence_from_score(score) == "LOW"
