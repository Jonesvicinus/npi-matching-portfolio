"""
Integration tests for the three matching scripts.
Uses patching to avoid needing real NPPES data — no DB access.

Critical correctness requirements covered:
  - Two-pass architecture: margin from BASE composites, not final
  - Same margin value for every candidate in a set
  - Uniqueness bonus applied only to rank-1
  - city_conflict detection (and clearing for anchor_approved)
  - Early-return rows (NO_STATE / NO_MATCH / SKIPPED) have blank v2 columns
  - All OUTPUT_FIELDS present in every returned row
  - Phase 2 anchor logic: approved vs inferred vs unanchored
  - anchor signal appended to ALL candidates
"""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import match_medical_centers as mmc
import match_medical_professionals as mmp
import match_professionals_phase2 as mmp2


# ---------------------------------------------------------------------------
# Row fixtures
# ---------------------------------------------------------------------------

def _org_row(npi, name, city="Springfield", state="IL", zip_="62701"):
    return {
        "npi": npi, "org_name": name,
        "practice_address1": "100 Hospital Way",
        "practice_city": city, "practice_state": state, "practice_zip": zip_,
        "practice_phone": "5551234567", "parent_org_name": None,
    }


def _ind_row(npi, first, last, credential="", taxonomy_code="",
             city="Springfield", state="IL", zip_="62701"):
    return {
        "npi": npi, "first_name": first, "last_name": last,
        "credential": credential, "taxonomy_code": taxonomy_code,
        "practice_address1": "200 Clinic Rd",
        "practice_city": city, "practice_state": state, "practice_zip": zip_,
        "practice_phone": "5559876543",
    }


def _center_hhl(name="Springfield Medical Center", state="Illinois",
                city="Springfield", zip_="62701", id_="c001"):
    return {"id": id_, "name": name, "location": state,
            "city": city, "zipcode": zip_, "url": ""}


def _prof_hhl(first="John", last="Smith", prof_type="Physician",
              center_id="c001", role_id="p001"):
    return {
        "role_ptr_id": role_id,
        "first_name": first, "last_name": last,
        "medical_professional_type": prof_type,
        "email": "john@hospital.org",
        "medical_center_id": center_id,
    }


def _center_lookup(center_id="c001", name="Springfield Medical Center",
                   state="IL", city="Springfield"):
    return {center_id: {"name": name, "state": state, "city": city}}


# ---------------------------------------------------------------------------
# TestCenterTwoPass
# ---------------------------------------------------------------------------

class TestCenterTwoPass(unittest.TestCase):

    def setUp(self):
        mmc._state_cache.clear()

    def _call(self, hhl_center, candidate_scores):
        """
        Run process_center with a fake state cache and controlled candidate scores.
        candidate_scores: list of (name_score, source_str, row_dict)
        """
        fake_cache = {
            "rows": [], "all_entries": [], "all_norm_names": [],
            "city_entries": {}, "zip_entries": {},
        }
        with patch.object(mmc, "get_state_cache", return_value=fake_cache), \
             patch.object(mmc, "score_against_entries", return_value=candidate_scores):
            return mmc.process_center(hhl_center)

    # --- Field completeness ---

    def test_output_fields_complete_on_match(self):
        row_a = _org_row("1111111111", "Springfield Medical Center")
        rows = self._call(_center_hhl(), [(0.9, "primary_name", row_a)])
        self.assertEqual(set(rows[0].keys()), set(mmc.OUTPUT_FIELDS))

    def test_output_fields_complete_on_no_state(self):
        rows = mmc.process_center(_center_hhl(state="Germany"))
        self.assertEqual(set(rows[0].keys()), set(mmc.OUTPUT_FIELDS))

    def test_output_fields_complete_on_no_match(self):
        fake_cache = {
            "rows": [], "all_entries": [], "all_norm_names": [],
            "city_entries": {}, "zip_entries": {},
        }
        with patch.object(mmc, "get_state_cache", return_value=fake_cache), \
             patch.object(mmc, "score_against_entries", return_value=[]):
            rows = mmc.process_center(_center_hhl())
        self.assertEqual(set(rows[0].keys()), set(mmc.OUTPUT_FIELDS))

    # --- Early-return rows have blank v2 columns ---

    def test_no_state_v2_columns_blank(self):
        rows = mmc.process_center(_center_hhl(state="Germany"))
        row = rows[0]
        self.assertEqual(row["confidence"], "NO_STATE")
        for col in ("candidate_strength", "name_score", "city_score", "zip_score",
                    "margin", "confidence_flags", "candidate_count"):
            self.assertEqual(row[col], "", f"{col!r} should be blank on NO_STATE")

    def test_no_match_v2_columns_blank(self):
        fake_cache = {
            "rows": [], "all_entries": [], "all_norm_names": [],
            "city_entries": {}, "zip_entries": {},
        }
        with patch.object(mmc, "get_state_cache", return_value=fake_cache), \
             patch.object(mmc, "score_against_entries", return_value=[]):
            rows = mmc.process_center(_center_hhl())
        row = rows[0]
        self.assertEqual(row["confidence"], "NO_MATCH")
        for col in ("candidate_strength", "name_score", "city_score", "zip_score",
                    "margin", "confidence_flags", "candidate_count"):
            self.assertEqual(row[col], "", f"{col!r} should be blank on NO_MATCH")

    # --- Single-candidate behavior ---

    def test_single_candidate_margin_blank(self):
        row_a = _org_row("1111111111", "Springfield Medical Center")
        rows = self._call(_center_hhl(), [(0.9, "primary_name", row_a)])
        self.assertEqual(rows[0]["margin"], "")

    def test_single_candidate_count_is_one(self):
        row_a = _org_row("1111111111", "Springfield Medical Center")
        rows = self._call(_center_hhl(), [(0.9, "primary_name", row_a)])
        self.assertEqual(str(rows[0]["candidate_count"]), "1")

    # --- Two-pass: margin from BASE composites, same for all, bonus only on rank-1 ---
    #
    # HHL: city=Springfield, zip=62701
    # Row A  name=0.8, city=Springfield (match), zip=99999 (no match)
    #   base_c = 0.8*0.60 + 1.0*0.20 + 0.0*0.20 = 0.680
    # Row B  name=0.6, city=Chicago   (no match), zip=88888 (no match)
    #   base_c = 0.6*0.60 + 0.0*0.20 + 0.0*0.20 = 0.360
    # margin_from_base  = 0.680 - 0.360 = 0.320   (is_unique: True, >=0.15)
    # rank-1 final      = min(1.0, 0.680 + 0.05)  = 0.730
    # rank-2 final      = 0.360  (no bonus)
    # margin_from_final = 0.730 - 0.360             = 0.370  (would be wrong)

    def _two_candidates(self):
        row_a = _org_row("1111111111", "Springfield MC",
                         city="Springfield", zip_="99999")
        row_b = _org_row("2222222222", "Springfield CH",
                         city="Chicago", zip_="88888")
        return self._call(
            _center_hhl(city="Springfield", zip_="62701"),
            [(0.8, "primary_name", row_a), (0.6, "primary_name", row_b)],
        )

    def test_margin_is_from_base_composites(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["margin"], "0.320",
                         "margin must be computed from base composites (0.680-0.360), "
                         "not final composites (0.730-0.360=0.370)")

    def test_same_margin_for_all_candidates(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["margin"], rows[1]["margin"])

    def test_uniqueness_bonus_only_on_rank1(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["match_score"], "0.730",
                         "rank-1 should get +0.05 uniqueness bonus (0.680+0.05)")
        self.assertEqual(rows[1]["match_score"], "0.360",
                         "rank-2 must NOT receive the uniqueness bonus")

    def test_candidate_strength_from_final_composite(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["candidate_strength"], "MODERATE")  # 0.730 in [0.65,0.85)
        self.assertEqual(rows[1]["candidate_strength"], "WEAK")       # 0.360 < 0.65

    def test_ranks_assigned_by_base_composite(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["rank"], 2)

    def test_candidate_count_matches_both(self):
        rows = self._two_candidates()
        self.assertEqual(str(rows[0]["candidate_count"]), "2")
        self.assertEqual(str(rows[1]["candidate_count"]), "2")

    # --- city_conflict ---

    def test_city_conflict_when_city_and_zip_both_mismatch(self):
        rows = self._two_candidates()
        # Row B: city=Chicago (not Springfield), zip=88888 (not 62701) → conflict
        self.assertIn("city_conflict", rows[1]["confidence_flags"])

    def test_no_city_conflict_when_city_matches(self):
        rows = self._two_candidates()
        # Row A: city=Springfield == HHL city → no conflict
        self.assertNotIn("city_conflict", rows[0]["confidence_flags"])

    def test_city_conflict_cleared_by_zip_match(self):
        # Row B has different city but same zip — zip match clears city_conflict
        row_a = _org_row("1111111111", "Springfield MC",
                         city="Springfield", zip_="62701")
        row_b = _org_row("2222222222", "Springfield H",
                         city="OtherCity", zip_="62701")  # city differs, zip same
        rows = self._call(
            _center_hhl(city="Springfield", zip_="62701"),
            [(0.9, "primary_name", row_a), (0.7, "primary_name", row_b)],
        )
        self.assertNotIn("city_conflict", rows[1]["confidence_flags"])

    # --- ZIP match level ---

    def test_zip5_signal_not_generic_zip(self):
        row_a = _org_row("3333333333", "Springfield MC",
                         city="Springfield", zip_="62701")
        rows = self._call(
            _center_hhl(city="Springfield", zip_="62701"),
            [(0.9, "primary_name", row_a)],
        )
        self.assertIn("zip5", rows[0]["signals_matched"])
        # Must NOT contain the old generic "zip" token
        tokens = rows[0]["signals_matched"].split("|")
        self.assertNotIn("zip", tokens)

    def test_zip9_signal_when_both_have_nine_digits(self):
        row_a = _org_row("4444444444", "Springfield MC",
                         city="Springfield", zip_="627011234")
        rows = self._call(
            _center_hhl(city="Springfield", zip_="627011234"),
            [(0.9, "primary_name", row_a)],
        )
        self.assertIn("zip9", rows[0]["signals_matched"])


# ---------------------------------------------------------------------------
# TestProfessionalPhase1TwoPass
# ---------------------------------------------------------------------------

class TestProfessionalPhase1TwoPass(unittest.TestCase):

    def setUp(self):
        mmp._state_cache.clear()

    def _call(self, prof, center_lookup, rows_and_scores):
        """
        Run process_professional with controlled name scores.
        rows_and_scores: list of (row_dict, name_score_float)
        """
        all_rows = [r for r, _ in rows_and_scores]
        last_names = [(r["last_name"] or "").lower() for r in all_rows]
        scores_by_npi = {r["npi"]: s for r, s in rows_and_scores}
        extract_result = [(None, 100, i) for i in range(len(all_rows))]

        def mock_score(row, last_name, name_expansions):
            return scores_by_npi[row["npi"]]

        with patch.object(mmp, "get_state_individuals",
                          return_value=(all_rows, last_names)), \
             patch("match_medical_professionals.fuzz_process") as mock_fp, \
             patch.object(mmp, "score_individual", side_effect=mock_score):
            mock_fp.extract.return_value = extract_result
            return mmp.process_professional((prof, center_lookup))

    # --- Field completeness ---

    def test_output_fields_complete_on_match(self):
        row_a = _ind_row("3333333333", "John", "Smith", credential="MD")
        rows = self._call(_prof_hhl(), _center_lookup(), [(row_a, 0.9)])
        self.assertEqual(set(rows[0].keys()), set(mmp.OUTPUT_FIELDS))

    def test_output_fields_complete_on_skipped(self):
        rows = mmp.process_professional(
            (_prof_hhl(prof_type="Administrator"), _center_lookup())
        )
        self.assertEqual(set(rows[0].keys()), set(mmp.OUTPUT_FIELDS))

    def test_output_fields_complete_on_no_match(self):
        with patch.object(mmp, "get_state_individuals", return_value=([], [])), \
             patch("match_medical_professionals.fuzz_process") as mock_fp:
            mock_fp.extract.return_value = []
            rows = mmp.process_professional((_prof_hhl(), _center_lookup()))
        self.assertEqual(set(rows[0].keys()), set(mmp.OUTPUT_FIELDS))

    # --- Early-return rows have blank v2 columns ---

    def test_skipped_v2_columns_blank(self):
        rows = mmp.process_professional(
            (_prof_hhl(prof_type="Administrator"), _center_lookup())
        )
        row = rows[0]
        self.assertEqual(row["confidence"], "SKIPPED")
        for col in ("candidate_strength", "name_score", "city_score",
                    "credential_score", "taxonomy_score",
                    "margin", "confidence_flags", "candidate_count"):
            self.assertEqual(row[col], "", f"{col!r} should be blank on SKIPPED")

    def test_no_match_v2_columns_blank(self):
        with patch.object(mmp, "get_state_individuals", return_value=([], [])), \
             patch("match_medical_professionals.fuzz_process") as mock_fp:
            mock_fp.extract.return_value = []
            rows = mmp.process_professional((_prof_hhl(), _center_lookup()))
        row = rows[0]
        self.assertEqual(row["confidence"], "NO_MATCH")
        for col in ("candidate_strength", "name_score", "city_score",
                    "credential_score", "taxonomy_score",
                    "margin", "confidence_flags", "candidate_count"):
            self.assertEqual(row[col], "", f"{col!r} should be blank on NO_MATCH")

    # --- Single-candidate ---

    def test_single_candidate_margin_blank(self):
        row_a = _ind_row("3333333333", "John", "Smith", credential="MD")
        rows = self._call(_prof_hhl(), _center_lookup(), [(row_a, 0.9)])
        self.assertEqual(rows[0]["margin"], "")

    # --- Two-pass: margin from BASE composites, same for all, bonus only on rank-1 ---
    #
    # Physician, center city=Springfield
    # Row A  name=0.9, city=Springfield (match), cred=MD (match), tax="" (no match)
    #   base_c = 0.9*0.50 + 1.0*0.20 + 1.0*0.15 + 0.0*0.15 = 0.800
    # Row B  name=0.6, city=Chicago (no match), cred="" (no match), tax="" (no match)
    #   base_c = 0.6*0.50 + 0.0*0.20 + 0.0*0.15 + 0.0*0.15 = 0.300
    # margin_from_base  = 0.800 - 0.300 = 0.500  (is_unique: True)
    # rank-1 final      = min(1.0, 0.800 + 0.05) = 0.850
    # rank-2 final      = 0.300  (no bonus)
    # margin_from_final = 0.850 - 0.300           = 0.550  (would be wrong)

    def _two_candidates(self):
        row_a = _ind_row("3333333333", "John", "Smith",
                         credential="MD", taxonomy_code="",
                         city="Springfield")
        row_b = _ind_row("4444444444", "Jane", "Smith",
                         credential="", taxonomy_code="",
                         city="Chicago")
        return self._call(
            _prof_hhl(prof_type="Physician"),
            _center_lookup(city="Springfield"),
            [(row_a, 0.9), (row_b, 0.6)],
        )

    def test_margin_is_from_base_composites(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["margin"], "0.500",
                         "margin must be from base composites (0.800-0.300), "
                         "not final composites (0.850-0.300=0.550)")

    def test_same_margin_for_all_candidates(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["margin"], rows[1]["margin"])

    def test_uniqueness_bonus_only_on_rank1(self):
        rows = self._two_candidates()
        self.assertEqual(rows[0]["match_score"], "0.850",
                         "rank-1 should get +0.05 bonus (0.800+0.05)")
        self.assertEqual(rows[1]["match_score"], "0.300",
                         "rank-2 must NOT receive the uniqueness bonus")

    def test_candidate_count_matches_candidates(self):
        rows = self._two_candidates()
        self.assertEqual(str(rows[0]["candidate_count"]), "2")
        self.assertEqual(str(rows[1]["candidate_count"]), "2")

    # --- city_conflict ---

    def test_city_conflict_when_city_present_both_sides_and_mismatch(self):
        rows = self._two_candidates()
        # Row B: center city=Springfield, NPPES city=Chicago → conflict
        self.assertIn("city_conflict", rows[1]["confidence_flags"])

    def test_no_city_conflict_on_city_match(self):
        rows = self._two_candidates()
        self.assertNotIn("city_conflict", rows[0]["confidence_flags"])

    def test_city_missing_suppresses_city_conflict(self):
        row_a = _ind_row("3333333333", "John", "Smith",
                         credential="MD", city="Chicago")
        cl = {"c001": {"name": "Unnamed Center", "state": "IL", "city": ""}}
        rows = self._call(_prof_hhl(prof_type="Physician"), cl, [(row_a, 0.9)])
        self.assertNotIn("city_conflict", rows[0]["confidence_flags"])

    # --- field scores present on match rows ---

    def test_field_scores_populated_on_match(self):
        rows = self._two_candidates()
        for col in ("name_score", "city_score", "credential_score", "taxonomy_score"):
            self.assertNotEqual(rows[0][col], "", f"{col!r} should not be blank on a match row")


# ---------------------------------------------------------------------------
# TestProfessionalPhase2AnchorLogic
# ---------------------------------------------------------------------------

class TestProfessionalPhase2AnchorLogic(unittest.TestCase):

    def setUp(self):
        mmp2._state_cache.clear()
        mmp2._loc_cache.clear()
        mmp2._center_loc_cache.clear()
        mmp2._center_approved.clear()
        mmp2._center_high.clear()

    _ANCHOR_LOC = {"practice_state": "IL",
                   "practice_city": "AnchorCity",
                   "practice_zip": "11111"}

    def _call_anchored(self, prof, center_lookup, rows_and_scores,
                       anchor_npi="5555555555", anchor_type="approved"):
        center_id = prof["medical_center_id"]
        if anchor_type == "approved":
            mmp2._center_approved[center_id] = anchor_npi
        else:
            mmp2._center_high[center_id] = anchor_npi

        all_rows = [r for r, _ in rows_and_scores]
        last_names = [(r["last_name"] or "").lower() for r in all_rows]
        scores_by_npi = {r["npi"]: s for r, s in rows_and_scores}
        extract_result = [(None, 100, i) for i in range(len(all_rows))]

        def mock_score(row, last_name, name_expansions):
            return scores_by_npi[row["npi"]]

        with patch.object(mmp2, "get_center_location",
                          return_value=self._ANCHOR_LOC), \
             patch.object(mmp2, "get_individuals_at_location",
                          return_value=(all_rows, last_names)), \
             patch("match_professionals_phase2.fuzz_process") as mock_fp, \
             patch.object(mmp2, "score_individual", side_effect=mock_score):
            mock_fp.extract.return_value = extract_result
            return mmp2.process_professional((prof, center_lookup))

    def _call_unanchored(self, prof, center_lookup, rows_and_scores):
        all_rows = [r for r, _ in rows_and_scores]
        last_names = [(r["last_name"] or "").lower() for r in all_rows]
        scores_by_npi = {r["npi"]: s for r, s in rows_and_scores}
        extract_result = [(None, 100, i) for i in range(len(all_rows))]

        def mock_score(row, last_name, name_expansions):
            return scores_by_npi[row["npi"]]

        with patch.object(mmp2, "get_state_individuals",
                          return_value=(all_rows, last_names)), \
             patch("match_professionals_phase2.fuzz_process") as mock_fp, \
             patch.object(mmp2, "score_individual", side_effect=mock_score):
            mock_fp.extract.return_value = extract_result
            return mmp2.process_professional((prof, center_lookup))

    # --- Field completeness ---

    def test_output_fields_complete_on_anchored_match(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)]
        )
        self.assertEqual(set(rows[0].keys()), set(mmp2.OUTPUT_FIELDS))

    def test_output_fields_complete_on_skipped(self):
        rows = mmp2.process_professional(
            (_prof_hhl(prof_type="Administrator"), _center_lookup())
        )
        self.assertEqual(set(rows[0].keys()), set(mmp2.OUTPUT_FIELDS))

    def test_skipped_v2_columns_blank(self):
        rows = mmp2.process_professional(
            (_prof_hhl(prof_type="Administrator"), _center_lookup())
        )
        row = rows[0]
        self.assertEqual(row["confidence"], "SKIPPED")
        for col in ("candidate_strength", "name_score", "city_score",
                    "credential_score", "taxonomy_score",
                    "margin", "confidence_flags", "candidate_count"):
            self.assertEqual(row[col], "", f"{col!r} should be blank on SKIPPED")

    # --- anchor_approved: city_val=1.0 AND city_conflict=False ---
    #
    # Row is in "OtherCity"; anchor_city from get_center_location = "AnchorCity".
    # Without anchor_approved: city mismatch → city_conflict=True and city credit=0.
    # With anchor_approved: human confirmed the center NPI, so both are overridden.

    def test_anchor_approved_no_city_conflict(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)],
            anchor_type="approved",
        )
        self.assertNotIn("city_conflict", rows[0]["confidence_flags"])

    def test_anchor_approved_gives_city_credit(self):
        # city_val=1.0 forced → city_score="1.000"
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)],
            anchor_type="approved",
        )
        self.assertEqual(rows[0]["city_score"], "1.000")

    def test_anchor_approved_match_score_reflects_city_credit(self):
        # name=0.9, city_val=1.0 (forced), cred=MD (match), tax="" (no match)
        # base_c = 0.9*0.50 + 1.0*0.20 + 1.0*0.15 + 0.0*0.15 = 0.800 (single, no bonus)
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         taxonomy_code="", city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)],
            anchor_type="approved",
        )
        self.assertEqual(rows[0]["match_score"], "0.800")

    # --- anchor_inferred: normal city logic applies → city_conflict can fire ---

    def test_anchor_inferred_allows_city_conflict(self):
        # anchor_city="AnchorCity", row city="OtherCity" → mismatch → conflict
        row_a = _ind_row("8888888888", "John", "Smith", credential="MD",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)],
            anchor_type="inferred",
        )
        self.assertIn("city_conflict", rows[0]["confidence_flags"])

    def test_anchor_inferred_lower_score_than_approved(self):
        # anchor_inferred: city_val=0.0 (mismatch)
        # name=0.9, city_val=0.0, cred=MD (match), tax="" (no match)
        # base_c = 0.9*0.50 + 0.0*0.20 + 1.0*0.15 + 0.0*0.15 = 0.600
        row_a = _ind_row("8888888888", "John", "Smith", credential="MD",
                         taxonomy_code="", city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(), [(row_a, 0.9)],
            anchor_type="inferred",
        )
        self.assertEqual(rows[0]["match_score"], "0.600")

    # --- anchor signal appended to ALL candidates ---

    def test_anchor_approved_signal_on_all_candidates(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="AnchorCity")
        row_b = _ind_row("9999999999", "Jon", "Smith", credential="",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(),
            [(row_a, 0.9), (row_b, 0.6)],
            anchor_type="approved",
        )
        self.assertIn("anchor_approved", rows[0]["signals_matched"])
        self.assertIn("anchor_approved", rows[1]["signals_matched"])

    def test_anchor_inferred_signal_on_all_candidates(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="AnchorCity")
        row_b = _ind_row("9999999999", "Jon", "Smith", credential="",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(),
            [(row_a, 0.9), (row_b, 0.6)],
            anchor_type="inferred",
        )
        self.assertIn("anchor_inferred", rows[0]["signals_matched"])
        self.assertIn("anchor_inferred", rows[1]["signals_matched"])

    def test_no_anchor_signal_without_confirmed_center(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="Springfield")
        rows = self._call_unanchored(_prof_hhl(), _center_lookup(), [(row_a, 0.9)])
        signals = rows[0]["signals_matched"]
        self.assertNotIn("anchor_approved", signals)
        self.assertNotIn("anchor_inferred", signals)

    # --- Two-pass margin same for all candidates (phase2) ---

    def test_same_margin_for_all_candidates_anchored(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="AnchorCity")
        row_b = _ind_row("9999999999", "Jon", "Smith", credential="",
                         city="OtherCity")
        rows = self._call_anchored(
            _prof_hhl(), _center_lookup(),
            [(row_a, 0.9), (row_b, 0.5)],
            anchor_type="approved",
        )
        self.assertEqual(rows[0]["margin"], rows[1]["margin"])

    # --- Fallback to state search when no anchor ---

    def test_no_anchor_falls_back_to_state_search(self):
        row_a = _ind_row("7777777777", "John", "Smith", credential="MD",
                         city="Springfield")
        rows = self._call_unanchored(_prof_hhl(), _center_lookup(), [(row_a, 0.9)])
        self.assertEqual(rows[0]["action"], "REVIEW")
        self.assertEqual(rows[0]["nppes_npi"], "7777777777")


if __name__ == "__main__":
    unittest.main()
