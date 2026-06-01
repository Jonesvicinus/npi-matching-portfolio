#!/usr/bin/env python3
"""
Run pipeline on sample CSVs (50 centers, 150 professionals).
Outputs go to the normal match files so the review site picks them up.
Originals are never touched.

Prerequisites: campaign_medicalcenter_sample.csv and
               campaign_medicalprofessional_enriched_sample.csv must exist in data/.

Run:
    cd pipeline
    python3 run_sample.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import match_medical_centers as mmc
import match_professionals_phase2 as mmp2

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

SAMPLE_CENTERS = os.path.join(DATA_DIR, "campaign_medicalcenter_sample.csv")
SAMPLE_PROFS   = os.path.join(DATA_DIR, "campaign_medicalprofessional_enriched_sample.csv")

for path in (SAMPLE_CENTERS, SAMPLE_PROFS):
    if not os.path.exists(path):
        print(f"ERROR: missing {path}")
        print("Create samples first — see README or run the sample-creation one-liner.")
        sys.exit(1)

mmc.INPUT_CSV = SAMPLE_CENTERS

# Professionals: sample the professionals list but keep the full centers CSV
# so every professional can resolve its center's state/city.
mmp2.PROF_CSV = SAMPLE_PROFS
# mmp2.CENTER_CSV stays as the full file (default)

print("=== Centers (50 rows) ===")
mmc.main()

print("\n=== Professionals phase 2 (150 rows) ===")
mmp2.main()

print("\nDone. Open http://localhost:5000 in the review site to inspect results.")
