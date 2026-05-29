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
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz, process as fuzz_process

from nicknames import expand_first_name
from scoring import (
    professional_composite,
    assess_candidate_strength, assess_selection_confidence,
)

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    "candidate_strength", "name_score", "city_score", "credential_score", "taxonomy_score", "phone_score",
    "margin", "confidence_flags", "candidate_count",
    "nppes_npi", "nppes_first_name", "nppes_last_name", "nppes_credential",
    "nppes_taxonomy_code", "nppes_address", "nppes_city", "nppes_state",
    "nppes_zip", "nppes_phone", "action",
]

# Per-state cache: { state_abbr: (rows, last_names) }
_state_cache      = {}
_state_cache_lock = threading.Lock()

# Email domain → center_id mapping (built in main())
_domain_center_map = {}


def _domain_from_email(email):
    """Return the lowercase domain portion of an email, or '' if none."""
    if "@" in email:
        return email.split("@", 1)[1].strip().lower()
    return ""


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


def normalize_phone(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[:10] if len(digits) >= 10 else ""


def compute_phone_match(hhl_phone, nppes_phone):
    """Returns (phone_match: bool, phone_score: float or None)."""
    h = normalize_phone(hhl_phone)
    n = normalize_phone(nppes_phone)
    if not h or not n:
        return False, None
    if h == n:
        return True, 1.0
    return False, 0.0


def score_individual(row, last_name, name_expansions):
    last_score  = fuzz.token_sort_ratio(last_name.lower(), (row["last_name"] or "").lower()) / 100.0
    first_score = max(
        fuzz.token_sort_ratio(exp.lower(), (row["first_name"] or "").lower()) / 100.0
        for exp in name_expansions
    )
    return last_score * 0.6 + first_score * 0.4



def process_professional(args):
    prof, center_lookup = args

    role_id    = prof["role_ptr_id"]
    first_name = re.sub(r"^dr\.?\s+", "", prof["first_name"].strip(), flags=re.IGNORECASE).strip()
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

    hhl_phone = prof.get("phone", "").strip()

    if first_name.lower() == "unknown" or last_name.lower() == "unknown":
        return [{**base, "rank": "", "confidence": "NOT_MATCHABLE", "match_score": "",
                 "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "",
                 "credential_score": "", "taxonomy_score": "", "phone_score": "",
                 "margin": "", "confidence_flags": "generic_placeholder", "candidate_count": "",
                 **empty, "action": "REVIEW"}]

    if prof_type not in MATCHABLE_TYPES or not first_name or not last_name or not state_abbr:
        return [{**base, "rank": "", "confidence": "SKIPPED", "match_score": "",
                 "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "",
                 "credential_score": "", "taxonomy_score": "", "phone_score": "",
                 "margin": "", "confidence_flags": "", "candidate_count": "",
                 **empty, "action": "SKIP"}]

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
        # First-name pre-filter: skip candidates whose best first-name similarity
        # is below 0.40 — they are clearly different people, not nickname variants.
        best_first = max(
            fuzz.token_sort_ratio(exp.lower(), (row["first_name"] or "").lower()) / 100.0
            for exp in name_expansions
        )
        if best_first < 0.40:
            continue
        score = score_individual(row, last_name, name_expansions)
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:TOP_N]

    if not top:
        return [{**base, "rank": "", "confidence": "NO_MATCH", "match_score": "",
                 "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "",
                 "credential_score": "", "taxonomy_score": "", "phone_score": "",
                 "margin": "", "confidence_flags": "", "candidate_count": "",
                 **empty, "action": "NEEDS_REVIEW"}]

    city_missing = not bool(center_city)

    # --- Pass 1: base composites ---
    intermediate = []
    for name_score, row in top:
        city_match = bool(
            center_city and row.get("practice_city") and
            center_city.strip().upper() == row["practice_city"].strip().upper()
        )
        cred_match  = credential_aligns(prof_type, row.get("credential", ""))
        tax_match   = taxonomy_matches(row, prefixes)
        city_val    = 1.0 if city_match else 0.0
        phone_match, phone_score = compute_phone_match(hhl_phone, row.get("practice_phone", ""))
        city_conflict = (
            not city_missing and
            bool(row.get("practice_city")) and
            not city_match
        )
        base_c, base_s, base_fs = professional_composite(
            name_score, city_val, cred_match, tax_match,
            city_missing=city_missing,
            is_unique=False,
            phone_match=phone_match,
        )
        intermediate.append({
            "name_score":        name_score,
            "row":               row,
            "city_val":          city_val,
            "cred_match":        cred_match,
            "tax_match":         tax_match,
            "phone_match":       phone_match,
            "phone_score":       phone_score,
            "city_conflict":     city_conflict,
            "base_composite":    base_c,
            "base_signals":      base_s,
            "base_field_scores": base_fs,
        })

    # Re-rank by base composite
    intermediate.sort(key=lambda x: x["base_composite"], reverse=True)
    candidate_count = len(intermediate)
    margin = (
        intermediate[0]["base_composite"] - intermediate[1]["base_composite"]
        if candidate_count >= 2 else None
    )
    is_unique = margin is not None and margin >= 0.15

    # Finalize: default final = base for all
    for c in intermediate:
        c["final_composite"]    = c["base_composite"]
        c["final_signals"]      = c["base_signals"]
        c["final_field_scores"] = c["base_field_scores"]

    # Recompute rank-1 with uniqueness bonus if applicable
    if is_unique:
        c0 = intermediate[0]
        fc, fs, ffs = professional_composite(
            c0["name_score"], c0["city_val"], c0["cred_match"], c0["tax_match"],
            city_missing=city_missing,
            is_unique=True,
            phone_match=c0["phone_match"],
        )
        c0["final_composite"]    = fc
        c0["final_signals"]      = fs
        c0["final_field_scores"] = ffs

    # --- Pass 2: build output rows ---
    out_rows = []
    for rank, c in enumerate(intermediate, 1):
        ffs = c["final_field_scores"]
        strength = assess_candidate_strength(c["final_composite"])
        confidence, conf_flags = assess_selection_confidence(
            c["final_composite"], c["city_conflict"], margin,
            name_score=ffs["name"],
        )
        out_rows.append({
            **base,
            "rank":                rank,
            "confidence":          confidence,
            "match_score":         f"{c['final_composite']:.3f}",
            "signals_matched":     "|".join(c["final_signals"]),
            "candidate_strength":  strength,
            "name_score":          f"{ffs['name']:.3f}",
            "city_score":          f"{ffs['city']:.3f}" if ffs["city"] is not None else "",
            "credential_score":    f"{ffs['credential']:.3f}",
            "taxonomy_score":      f"{ffs['taxonomy']:.3f}",
            "phone_score":         f"{c['phone_score']:.3f}" if c["phone_score"] is not None else "",
            "margin":              f"{margin:.3f}" if margin is not None else "",
            "confidence_flags":    "|".join(conf_flags),
            "candidate_count":     candidate_count,
            "nppes_npi":           c["row"]["npi"],
            "nppes_first_name":    c["row"]["first_name"]        or "",
            "nppes_last_name":     c["row"]["last_name"]         or "",
            "nppes_credential":    c["row"]["credential"]        or "",
            "nppes_taxonomy_code": c["row"]["taxonomy_code"]     or "",
            "nppes_address":       c["row"]["practice_address1"] or "",
            "nppes_city":          c["row"]["practice_city"]     or "",
            "nppes_state":         c["row"]["practice_state"]    or "",
            "nppes_zip":           c["row"]["practice_zip"]      or "",
            "nppes_phone":         c["row"]["practice_phone"]    or "",
            "action":              "REVIEW",
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

    # Build email domain → center_id map: majority-vote per domain across all professionals.
    # Used in phase 2 to upgrade inferred anchor to approved when email confirms center.
    from collections import Counter
    domain_votes = {}
    for p in professionals:
        domain = _domain_from_email(p.get("email", "").strip())
        if domain:
            cid = p["medical_center_id"].strip()
            domain_votes.setdefault(domain, Counter())[cid] += 1
    global _domain_center_map
    _domain_center_map = {
        domain: counter.most_common(1)[0][0]
        for domain, counter in domain_votes.items()
        if counter.most_common(1)[0][1] >= 2  # require at least 2 professionals to confirm domain
    }

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
