#!/usr/bin/env python3
"""
Show sample output rows for representative scoring scenarios.
No real NPPES data needed — uses the same patching approach as integration tests.
Run: python3 demo_scoring.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch
import match_medical_centers as mmc
import match_medical_professionals as mmp
import match_professionals_phase2 as mmp2


# ---------------------------------------------------------------------------
# Helpers (same as integration tests)
# ---------------------------------------------------------------------------

def _org_row(npi, name, city="Springfield", state="IL", zip_="62701"):
    return {"npi": npi, "org_name": name, "practice_address1": "100 Hospital Way",
            "practice_city": city, "practice_state": state, "practice_zip": zip_,
            "practice_phone": "5551234567", "parent_org_name": None}

def _ind_row(npi, first, last, credential="", taxonomy_code="",
             city="Springfield", state="IL", zip_="62701"):
    return {"npi": npi, "first_name": first, "last_name": last,
            "credential": credential, "taxonomy_code": taxonomy_code,
            "practice_address1": "200 Clinic Rd",
            "practice_city": city, "practice_state": state, "practice_zip": zip_,
            "practice_phone": "5559876543"}

def _fake_cache():
    return {"rows": [], "all_entries": [], "all_norm_names": [],
            "city_entries": {}, "zip_entries": {}}

def _run_center(hhl, candidates):
    mmc._state_cache.clear()
    with patch.object(mmc, "get_state_cache", return_value=_fake_cache()), \
         patch.object(mmc, "score_against_entries", return_value=candidates):
        return mmc.process_center(hhl)

def _run_prof(prof, cl, rows_scores):
    mmp._state_cache.clear()
    all_rows = [r for r, _ in rows_scores]
    last_names = [(r["last_name"] or "").lower() for r in all_rows]
    scores = {r["npi"]: s for r, s in rows_scores}
    with patch.object(mmp, "get_state_individuals", return_value=(all_rows, last_names)), \
         patch("match_medical_professionals.fuzz_process") as fp, \
         patch.object(mmp, "score_individual",
                      side_effect=lambda row, *_: scores[row["npi"]]):
        fp.extract.return_value = [(None, 100, i) for i in range(len(all_rows))]
        return mmp.process_professional((prof, cl))

def _run_prof2_anchored(prof, cl, rows_scores, anchor_npi, anchor_type="approved"):
    mmp2._state_cache.clear(); mmp2._loc_cache.clear()
    mmp2._center_loc_cache.clear(); mmp2._center_approved.clear(); mmp2._center_high.clear()
    if anchor_type == "approved":
        mmp2._center_approved[prof["medical_center_id"]] = anchor_npi
    else:
        mmp2._center_high[prof["medical_center_id"]] = anchor_npi
    all_rows = [r for r, _ in rows_scores]
    last_names = [(r["last_name"] or "").lower() for r in all_rows]
    scores = {r["npi"]: s for r, s in rows_scores}
    loc = {"practice_state": "IL", "practice_city": "AnchorCity", "practice_zip": "11111"}
    with patch.object(mmp2, "get_center_location", return_value=loc), \
         patch.object(mmp2, "get_individuals_at_location", return_value=(all_rows, last_names)), \
         patch("match_professionals_phase2.fuzz_process") as fp, \
         patch.object(mmp2, "score_individual",
                      side_effect=lambda row, *_: scores[row["npi"]]):
        fp.extract.return_value = [(None, 100, i) for i in range(len(all_rows))]
        return mmp2.process_professional((prof, cl))


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

CENTER_COLS = ["rank", "confidence", "candidate_strength", "match_score",
               "name_score", "city_score", "zip_score",
               "margin", "confidence_flags", "candidate_count",
               "signals_matched", "nppes_npi"]

PROF_COLS   = ["rank", "confidence", "candidate_strength", "match_score",
               "name_score", "city_score", "credential_score", "taxonomy_score",
               "margin", "confidence_flags", "candidate_count",
               "signals_matched", "nppes_npi"]

def show(label, rows, cols):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    for row in rows:
        print(f"\n  rank {row['rank']}  |  NPI {row.get('nppes_npi','')}")
        for col in cols:
            if col in ("rank", "nppes_npi"):
                continue
            val = row.get(col, "")
            print(f"    {col:<22} {val}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def main():
    hhl_c = lambda name, city, zip_, state="Illinois": {
        "id": "c1", "name": name, "location": state,
        "city": city, "zipcode": zip_, "url": ""}

    hhl_p = lambda first, last, ptype, cid="c1": {
        "role_ptr_id": "p1", "first_name": first, "last_name": last,
        "medical_professional_type": ptype, "email": "", "medical_center_id": cid}

    cl = {"c1": {"name": "Springfield Medical Center", "state": "IL", "city": "Springfield"}}

    # ------------------------------------------------------------------
    # CENTERS
    # ------------------------------------------------------------------

    print("\n" + "#"*70)
    print("# CENTERS")
    print("#"*70)

    # Scenario 1: perfect match (name + city + zip9), single candidate
    rows = _run_center(
        hhl_c("Springfield Medical Center", "Springfield", "627011234"),
        [(1.0, "primary_name",
          _org_row("1000000001", "Springfield Medical Center",
                   city="Springfield", zip_="627011234"))]
    )
    show("Center — perfect match (name+city+zip9, single candidate)", rows, CENTER_COLS)

    # Scenario 2: two candidates, clear winner → uniqueness bonus fires
    rows = _run_center(
        hhl_c("Springfield Medical Center", "Springfield", "62701"),
        [(0.8, "primary_name",
          _org_row("1000000001", "Springfield MC", city="Springfield", zip_="99999")),
         (0.6, "primary_name",
          _org_row("1000000002", "Springfield CH", city="Chicago",    zip_="88888"))]
    )
    show("Center — 2 candidates, clear winner (margin=0.320, uniqueness fires)", rows, CENTER_COLS)

    # Scenario 3: two close candidates → margin_too_close
    rows = _run_center(
        hhl_c("Springfield Medical Center", "Springfield", "62701"),
        [(0.8, "primary_name",
          _org_row("1000000001", "Springfield MC", city="Springfield", zip_="99999")),
         (0.77, "primary_name",
          _org_row("1000000002", "Springfield CH", city="Springfield", zip_="88888"))]
    )
    show("Center — 2 close candidates (margin<0.08, margin_too_close fires for both)", rows, CENTER_COLS)

    # Scenario 4: city conflict (mismatch, no zip match)
    rows = _run_center(
        hhl_c("Springfield Medical Center", "Springfield", "62701"),
        [(0.9, "primary_name",
          _org_row("1000000001", "Springfield Medical Center",
                   city="Chicago", zip_="60601"))]
    )
    show("Center — city conflict (HHL=Springfield, NPPES=Chicago, zip mismatch)", rows, CENTER_COLS)

    # Scenario 5: NO_STATE
    show("Center — NO_STATE (location not a US state)",
         mmc.process_center(hhl_c("Springfield Medical Center", "", "00000", state="Germany")),
         CENTER_COLS)

    # ------------------------------------------------------------------
    # PROFESSIONALS
    # ------------------------------------------------------------------

    print("\n" + "#"*70)
    print("# PROFESSIONALS")
    print("#"*70)

    # Scenario 6: strong professional match
    rows = _run_prof(
        hhl_p("John", "Smith", "Physician"),
        cl,
        [(_ind_row("2000000001", "John", "Smith", credential="MD",
                   taxonomy_code="207Q00000X", city="Springfield"), 0.95)]
    )
    show("Professional — strong match (name=0.95, city+cred+tax all match)", rows, PROF_COLS)

    # Scenario 7: professional, two candidates, clear winner
    rows = _run_prof(
        hhl_p("John", "Smith", "Physician"),
        cl,
        [(_ind_row("2000000001", "John", "Smith", credential="MD",   city="Springfield"), 0.9),
         (_ind_row("2000000002", "Jane", "Smith", credential="",     city="Chicago"),     0.6)]
    )
    show("Professional — 2 candidates, clear winner (margin=0.500, bonus fires)", rows, PROF_COLS)

    # Scenario 8: professional missing city → ceiling 0.75
    rows = _run_prof(
        hhl_p("John", "Smith", "Physician"),
        {"c1": {"name": "Springfield MC", "state": "IL", "city": ""}},  # no city
        [(_ind_row("2000000001", "John", "Smith", credential="MD",
                   taxonomy_code="207Q00000X", city="Springfield"), 0.95)]
    )
    show("Professional — no HHL city (city_missing ceiling 0.75)", rows, PROF_COLS)

    # Scenario 9: SKIPPED (non-matchable type)
    rows = mmp.process_professional(
        (hhl_p("John", "Smith", "Administrator"), cl)
    )
    show("Professional — SKIPPED (non-matchable type)", rows, PROF_COLS)

    # ------------------------------------------------------------------
    # PHASE 2 ANCHORING
    # ------------------------------------------------------------------

    print("\n" + "#"*70)
    print("# PHASE 2 ANCHORING")
    print("#"*70)

    # Scenario 10: anchor_approved — city credit forced, no conflict
    rows = _run_prof2_anchored(
        hhl_p("John", "Smith", "Physician"),
        cl,
        [(_ind_row("3000000001", "John", "Smith", credential="MD",
                   city="OtherCity"), 0.9)],    # city differs from anchor
        anchor_npi="9999999999", anchor_type="approved"
    )
    show("Phase2 — anchor_approved (city=OtherCity but city credit forced, no conflict)", rows, PROF_COLS)

    # Scenario 11: anchor_inferred — normal city logic, conflict fires
    rows = _run_prof2_anchored(
        hhl_p("John", "Smith", "Physician"),
        cl,
        [(_ind_row("3000000001", "John", "Smith", credential="MD",
                   city="OtherCity"), 0.9)],
        anchor_npi="9999999999", anchor_type="inferred"
    )
    show("Phase2 — anchor_inferred (city=OtherCity, city_conflict fires)", rows, PROF_COLS)

    print()


if __name__ == "__main__":
    main()
