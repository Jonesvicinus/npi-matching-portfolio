#!/usr/bin/env python3
"""
Post-processing validation: independently confirms professional matches by
querying the NPPES public API and checking whether our matched NPI appears
in the results for that name + state.

Reads:  data/medical_professional_matches_phase2.csv  (or phase1 if phase2 absent)
Writes: data/medical_professional_matches_validated.csv  (adds api_confirmed column)

api_confirmed values:
  YES        — our matched NPI appeared in the NPPES API results
  NO         — API returned results but our NPI was not among them
  NO_RESULTS — API returned zero results for this name + state
  ERROR      — API call failed (timeout, network error, etc.)
  SKIPPED    — not queried (rank > 1, confidence LOW/SKIPPED/NO_MATCH, or no NPI)

Requires internet access. Runs at ~2 requests/second to stay within NPPES rate limits.
On the full 12k dataset expect ~4,000-6,000 queries (~30-60 minutes).

Run:
    python3 validate_matches.py
    python3 validate_matches.py --limit 100   # validate first 100 rank-1 matches only
"""

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(BASE_DIR, "data")
PHASE2_CSV = os.path.join(DATA_DIR, "medical_professional_matches_phase2.csv")
PHASE1_CSV = os.path.join(DATA_DIR, "medical_professional_matches.csv")
OUTPUT_CSV = os.path.join(DATA_DIR, "medical_professional_matches_validated.csv")

NPPES_API_URL  = "https://npiregistry.cms.hhs.gov/api/"
REQUEST_DELAY  = 0.5   # seconds between requests
REQUEST_TIMEOUT = 10   # seconds per request


def query_nppes(first_name, last_name, state):
    """
    Query NPPES API for individual providers matching name + state.
    Returns a set of NPI strings, or None on error.
    """
    params = {
        "version":          "2.1",
        "first_name":       first_name,
        "last_name":        last_name,
        "state":            state,
        "enumeration_type": "NPI-1",
        "limit":            "20",
    }
    url = NPPES_API_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data   = json.loads(resp.read().decode("utf-8"))
            npis   = {str(r.get("number", "")) for r in data.get("results", [])}
            return npis
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0,
                        help="Only validate the first N rank-1 matches (0 = all)")
    args = parser.parse_args()

    input_csv = PHASE2_CSV if os.path.exists(PHASE2_CSV) else PHASE1_CSV
    if not os.path.exists(input_csv):
        print(f"ERROR: no match CSV found in {DATA_DIR}")
        return

    print(f"Reading {input_csv}")
    with open(input_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("No rows found.")
        return

    fieldnames = list(rows[0].keys())
    if "api_confirmed" not in fieldnames:
        fieldnames.append("api_confirmed")

    queried   = 0
    yes_count = 0
    no_count  = 0
    no_result = 0
    err_count = 0

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            should_query = (
                row.get("rank") == "1" and
                row.get("confidence") in ("HIGH", "MEDIUM") and
                row.get("nppes_npi") and
                row.get("hhl_state")
            )

            if args.limit and queried >= args.limit:
                should_query = False

            if not should_query:
                row["api_confirmed"] = "SKIPPED"
                writer.writerow(row)
                continue

            npis = query_nppes(
                row["hhl_first_name"],
                row["hhl_last_name"],
                row["hhl_state"],
            )
            queried += 1
            time.sleep(REQUEST_DELAY)

            if npis is None:
                row["api_confirmed"] = "ERROR"
                err_count += 1
            elif not npis:
                row["api_confirmed"] = "NO_RESULTS"
                no_result += 1
            elif row["nppes_npi"] in npis:
                row["api_confirmed"] = "YES"
                yes_count += 1
            else:
                row["api_confirmed"] = "NO"
                no_count += 1

            writer.writerow(row)

            if queried % 100 == 0:
                print(f"  {queried} queried — YES:{yes_count} NO:{no_count} "
                      f"NO_RESULTS:{no_result} ERROR:{err_count}", flush=True)

    print(f"\nDone — {queried} matches queried.")
    print(f"  YES        : {yes_count}  (API confirms our NPI)")
    print(f"  NO         : {no_count}   (API found the name but different NPI)")
    print(f"  NO_RESULTS : {no_result}  (name not found in state — stale/missing NPPES record)")
    print(f"  ERROR      : {err_count}")
    print(f"\nOutput: {OUTPUT_CSV}")
    print("Note: 'NO' results warrant manual review — a different NPI exists for this name.")


if __name__ == "__main__":
    main()
