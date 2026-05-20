#!/usr/bin/env python3
"""
Match HHL medical professionals against NPPES individual providers using local SQLite DB.
Multi-signal confidence: HIGH requires name (>=0.85) + city match + credential alignment.

Note on accuracy: even with all three signals, professionals are harder to match uniquely
than centers (common names, shared cities). Phase 2 will add center-anchor matching
(narrowing search to providers at a confirmed center's NPI address) for higher precision.

Prerequisites:
    python3 build_local_db.py   (one-time)

Run:
    python3 match_medical_professionals.py
"""

import csv
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz, process as fuzz_process

from nicknames import expand_first_name

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
DB_PATH    = os.path.join(DATA_DIR, "nppes_local.db")
PROF_CSV   = os.path.join(DATA_DIR, "campaign_medicalprofessional_enriched.csv")
CENTER_CSV = os.path.join(DATA_DIR, "campaign_medicalcenter.csv")
OUTPUT_CSV = os.path.join(DATA_DIR, "medical_professional_matches.csv")

WORKERS = 12
TOP_N   = 5

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
    "Puerto Rico": "PR", "Guam": "GU", "Virgin Islands": "VI",
}

TAXONOMY_PREFIXES = {
    "Physician":                           ["207", "208", "2086", "2084", "2085"],
    "Nurse":                               ["163W", "364S", "364SA", "376G", "376J"],
    "Social Worker":                       ["1041"],
    "Rehabilitation Therapist (PT/OT/ET)": ["2251", "225X", "225T"],
    "Transplant Coordinator":              ["163W", "364S", "207"],
    "Case Manager":                        ["163W", "1041", "207"],
    "Catastrophic Injury Contact":         [],
    "Financial Coordinator":               [],
    "Administrator":                       [],
    "Other professional type":             [],
    "Referral":                            [],
}

MATCHABLE_TYPES = {
    "Physician", "Nurse", "Social Worker",
    "Rehabilitation Therapist (PT/OT/ET)",
    "Transplant Coordinator", "Case Manager",
}

# Keywords to look for in NPPES credential field per professional type
CREDENTIAL_KEYWORDS = {
    "Physician":                           ["MD", "DO", "MBBS", "M.D", "D.O"],
    "Nurse":                               ["RN", "NP", "APRN", "FNP", "CNP", "CRNP", "APN"],
    "Social Worker":                       ["LCSW", "MSW", "LISW", "LMSW", "CSW"],
    "Rehabilitation Therapist (PT/OT/ET)": ["PT", "OT", "DPT", "MPT", "LPT"],
    "Transplant Coordinator":              ["RN", "NP", "APRN", "BSN"],
    "Case Manager":                        ["RN", "LCSW", "MSW"],
}

OUTPUT_FIELDS = [
    "hhl_role_id", "hhl_first_name", "hhl_last_name", "hhl_type", "hhl_email",
    "hhl_medical_center_id", "hhl_medical_center_name", "hhl_state", "hhl_city",
    "rank", "confidence", "match_score", "signals_matched",
    "nppes_npi", "nppes_first_name", "nppes_last_name", "nppes_credential",
    "nppes_taxonomy_code", "nppes_address", "nppes_city", "nppes_state",
    "nppes_zip", "nppes_phone", "action",
]

# Per-state cache: { state_abbr: (rows, last_names) }
_state_cache      = {}
_state_cache_lock = threading.Lock()


def load_state_individuals(state_abbr):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    cur.execute("""
        SELECT p.npi, p.first_name, p.last_name, p.credential,
               p.practice_address1, p.practice_city, p.practice_state,
               p.practice_zip, p.practice_phone,
               t.taxonomy_code
        FROM providers p
        LEFT JOIN taxonomies t ON t.npi = p.npi AND t.is_primary = 1
        WHERE p.entity_type = 1
          AND p.practice_state = ?
          AND p.deactivation_date IS NULL
    """, (state_abbr,))
    rows       = [dict(r) for r in cur.fetchall()]
    last_names = [(r["last_name"] or "").lower() for r in rows]
    conn.close()
    return rows, last_names


def get_state_individuals(state_abbr):
    with _state_cache_lock:
        if state_abbr not in _state_cache:
            _state_cache[state_abbr] = load_state_individuals(state_abbr)
        return _state_cache[state_abbr]


def taxonomy_matches(row, prefixes):
    if not prefixes:
        return True
    code = row.get("taxonomy_code") or ""
    return any(code.startswith(p) for p in prefixes)


def credential_aligns(prof_type, credential):
    if not credential:
        return False
    keywords = CREDENTIAL_KEYWORDS.get(prof_type, [])
    cred_upper = credential.upper()
    return any(kw in cred_upper for kw in keywords)


def score_individual(row, last_name, name_expansions):
    last_score  = fuzz.token_sort_ratio(last_name.lower(), (row["last_name"] or "").lower()) / 100.0
    first_score = max(
        fuzz.token_sort_ratio(exp.lower(), (row["first_name"] or "").lower()) / 100.0
        for exp in name_expansions
    )
    return last_score * 0.6 + first_score * 0.4


def confidence_and_signals(name_score, city_match, cred_match, tax_match):
    """
    Multi-signal confidence for professionals.

    HIGH   = name >= 0.85 AND city matches AND credential aligns AND taxonomy matches.
             All four signals must fire. Even so, phase 2 center-anchor matching
             is needed for true 100% certainty on professionals.
    MEDIUM = name >= 0.80 AND city matches AND (credential OR taxonomy matches).
    LOW    = name >= 0.65 with at least one supporting signal.
    """
    signals = []

    if name_score >= 0.85:
        signals.append("name")
    elif name_score >= 0.80:
        signals.append("name_good")

    if city_match:
        signals.append("city")
    if cred_match:
        signals.append("credential")
    if tax_match:
        signals.append("taxonomy")

    if name_score >= 0.85 and city_match and cred_match and tax_match:
        return "HIGH", "|".join(signals)

    if name_score >= 0.80 and city_match and (cred_match or tax_match):
        return "MEDIUM", "|".join(signals)

    if name_score >= 0.65 and (city_match or cred_match or tax_match):
        return "LOW", "|".join(signals)

    if name_score >= 0.65:
        return "LOW", "|".join(signals) if signals else "name_only"

    return "LOW", "|".join(signals) if signals else ""


def process_professional(args):
    prof, center_lookup = args

    role_id    = prof["role_ptr_id"]
    first_name = prof["first_name"].strip()
    last_name  = prof["last_name"].strip()
    prof_type  = prof.get("medical_professional_type", "").strip()
    email      = prof.get("email", "").strip()
    center_id  = prof["medical_center_id"].strip()

    center      = center_lookup.get(center_id, {})
    center_name = center.get("name", "")
    state_abbr  = center.get("state")
    center_city = center.get("city", "")

    base = {
        "hhl_role_id":             role_id,
        "hhl_first_name":          first_name,
        "hhl_last_name":           last_name,
        "hhl_type":                prof_type,
        "hhl_email":               email,
        "hhl_medical_center_id":   center_id,
        "hhl_medical_center_name": center_name,
        "hhl_state":               state_abbr or "",
        "hhl_city":                center_city,
    }
    empty = {
        "nppes_npi": "", "nppes_first_name": "", "nppes_last_name": "",
        "nppes_credential": "", "nppes_taxonomy_code": "",
        "nppes_address": "", "nppes_city": "", "nppes_state": "",
        "nppes_zip": "", "nppes_phone": "",
    }

    if prof_type not in MATCHABLE_TYPES or not first_name or not last_name or not state_abbr:
        return [{**base, "rank": "", "confidence": "SKIPPED", "match_score": "",
                 "signals_matched": "", **empty, "action": "SKIP"}]

    name_expansions = expand_first_name(first_name)
    name_expansions = (name_expansions + [first_name] * 5)[:5]

    prefixes             = TAXONOMY_PREFIXES.get(prof_type, [])
    all_rows, last_names = get_state_individuals(state_abbr)

    # rapidfuzz pre-filter on last name in C++, then full score in Python
    last_results      = fuzz_process.extract(
        last_name.lower(), last_names,
        scorer=fuzz.token_sort_ratio,
        limit=200,
        score_cutoff=40,
    )
    candidate_indices = {idx for _, _, idx in last_results}

    scored = []
    for idx in candidate_indices:
        row = all_rows[idx]
        # Only exclude if taxonomy is explicitly set AND doesn't match.
        # NULL taxonomy means the provider hasn't filed one — don't discard them.
        code = row.get("taxonomy_code") or ""
        if prefixes and code and not any(code.startswith(p) for p in prefixes):
            continue
        score = score_individual(row, last_name, name_expansions)
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:TOP_N]

    if not top:
        return [{**base, "rank": "", "confidence": "NO_MATCH", "match_score": "",
                 "signals_matched": "", **empty, "action": "NEEDS_REVIEW"}]

    out_rows = []
    for rank, (score, row) in enumerate(top, 1):
        city_match = bool(
            center_city and row.get("practice_city") and
            center_city.strip().upper() == row["practice_city"].strip().upper()
        )
        cred_match = credential_aligns(prof_type, row.get("credential", ""))
        tax_match  = taxonomy_matches(row, prefixes)

        confidence, signals = confidence_and_signals(score, city_match, cred_match, tax_match)

        out_rows.append({
            **base,
            "rank":                rank,
            "confidence":          confidence,
            "match_score":         f"{score:.3f}",
            "signals_matched":     signals,
            "nppes_npi":           row["npi"],
            "nppes_first_name":    row["first_name"]        or "",
            "nppes_last_name":     row["last_name"]         or "",
            "nppes_credential":    row["credential"]        or "",
            "nppes_taxonomy_code": row["taxonomy_code"]     or "",
            "nppes_address":       row["practice_address1"] or "",
            "nppes_city":          row["practice_city"]     or "",
            "nppes_state":         row["practice_state"]    or "",
            "nppes_zip":           row["practice_zip"]      or "",
            "nppes_phone":         row["practice_phone"]    or "",
            "action":              "REVIEW",  # never auto-approve — human confirms first
        })
    return out_rows


def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run: python3 build_local_db.py")
        return

    print("Loading medical centers...")
    center_lookup = {}
    with open(CENTER_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            center_lookup[row["id"]] = {
                "name":  row["name"].strip(),
                "state": STATE_ABBR.get(row["location"].strip()),
                "city":  row.get("city", "").strip(),
            }

    print("Loading professionals...")
    with open(PROF_CSV, encoding="utf-8") as f:
        professionals = list(csv.DictReader(f))

    total = len(professionals)
    print(f"Matching {total} professionals using {WORKERS} workers...\n")

    args      = [(p, center_lookup) for p in professionals]
    completed = 0

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for result_rows in executor.map(process_professional, args):
                writer.writerows(result_rows)
                completed += 1
                if completed % 500 == 0:
                    print(f"  {completed}/{total} processed...", flush=True)

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO_MATCH": 0, "SKIPPED": 0}
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["rank"] in ("", "1"):
                c = row["confidence"]
                if c in counts:
                    counts[c] += 1

    print(f"\nDone — {total} professionals processed.")
    for label, n in counts.items():
        print(f"  {label:10}: {n}")
    print(f"\nSignals guide: name = name>=0.85 | city = city matched | credential = credential aligns | taxonomy = taxonomy matches")
    print(f"HIGH requires all four signals")
    print(f"\nOutput: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
