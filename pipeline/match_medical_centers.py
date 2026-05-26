#!/usr/bin/env python3
"""
Match HHL medical centers against NPPES org providers using local SQLite DB.
Multi-signal confidence: HIGH requires name (>=0.85) + city match + ZIP match.

Speed design:
- One SQL query per state (not per center) — loaded once and cached
- City and ZIP indexes built in-memory at cache load time
- Per-center lookup is O(1) dictionary access + rapidfuzz on a small subset
- Name normalization applied at load time (abbreviation expansion, punctuation removal)

Prerequisites:
    python3 build_local_db.py   (one-time)

Run:
    python3 match_medical_centers.py
"""

import csv
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz, process as fuzz_process
from scoring import center_composite, confidence_from_score

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
DB_PATH    = os.path.join(DATA_DIR, "nppes_local.db")
INPUT_CSV  = os.path.join(DATA_DIR, "campaign_medicalcenter.csv")
OUTPUT_CSV = os.path.join(DATA_DIR, "medical_center_matches.csv")

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

OUTPUT_FIELDS = [
    "hhl_id", "hhl_name", "hhl_state", "hhl_city", "hhl_zip", "hhl_url",
    "rank", "confidence", "match_score", "match_source", "signals_matched",
    "nppes_npi", "nppes_name", "nppes_address", "nppes_city",
    "nppes_state", "nppes_zip", "nppes_phone", "nppes_parent_org", "action",
]

# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Applied to both HHL names and NPPES names before any comparison.
# Order matters — longer patterns first.
_ABBREV_PATTERNS = [
    (r"\bchildren's\b",       "childrens"),
    (r"\bchildren\b",         "childrens"),
    (r"\bst\.?\s+",           "saint "),
    (r"\bmt\.?\s+",           "mount "),
    (r"\bft\.?\s+",           "fort "),
    (r"\bgen\.?\b",           "general"),
    (r"\bhosp\.?\b",          "hospital"),
    (r"\bmed\.?\b",           "medical"),
    (r"\buniv\.?\b",          "university"),
    (r"\bctr\.?\b",           "center"),
    (r"\bdept\.?\b",          "department"),
    (r"\bhlth\.?\b",          "health"),
    (r"\bsys\.?\b",           "system"),
    (r"\bmem\.?\b",           "memorial"),
    (r"\bpresby\.?\b",        "presbyterian"),
    (r"\brehab\.?\b",         "rehabilitation"),
    (r"\borthop(aedic)?\.?\b","orthopedic"),
    (r"\bneurol\.?\b",        "neurological"),
    (r"\bpeds\.?\b",          "pediatric"),
    (r"\bpediatrics\b",       "pediatric"),
    (r"\bpsych\.?\b",         "psychiatric"),
    (r"\bregl\.?\b",          "regional"),
    (r"\bnatl\.?\b",          "national"),
    (r"\bintl\.?\b",          "international"),
]
_STRIP_SUFFIXES = [
    r",?\s*\binc\.?\b", r",?\s*\bllc\.?\b", r",?\s*\bcorp\.?\b",
    r",?\s*\bltd\.?\b", r",?\s*\blp\.?\b",
]
_ABBREV_RE   = [(re.compile(p, re.IGNORECASE), r) for p, r in _ABBREV_PATTERNS]
_SUFFIX_RE   = [re.compile(p, re.IGNORECASE)      for p    in _STRIP_SUFFIXES]
_PUNCT_RE    = re.compile(r"[^\w\s]")
_SPACES_RE   = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    for pat, repl in _ABBREV_RE:
        n = pat.sub(repl, n)
    for pat in _SUFFIX_RE:
        n = pat.sub("", n)
    n = _PUNCT_RE.sub(" ", n)
    n = _SPACES_RE.sub(" ", n).strip()
    return n


# ---------------------------------------------------------------------------
# State cache
# One SQL query per state. Builds city + ZIP indexes in-memory at load time.
# ---------------------------------------------------------------------------

_state_cache      = {}
_state_cache_lock = threading.Lock()


def load_state_cache(state_abbr):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    cur.execute("""
        SELECT p.npi, p.org_name, p.practice_address1, p.practice_city,
               p.practice_state, p.practice_zip, p.practice_phone, p.parent_org_name,
               GROUP_CONCAT(n.other_name, '||') AS other_names
        FROM providers p
        LEFT JOIN other_names n ON n.npi = p.npi
        WHERE p.entity_type = 2
          AND p.practice_state = ?
          AND p.deactivation_date IS NULL
        GROUP BY p.npi
    """, (state_abbr,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Build flat name entry list: (normalized_name, original_name, source, row_idx)
    all_entries = []
    for i, row in enumerate(rows):
        if row["org_name"]:
            all_entries.append((normalize_name(row["org_name"]), row["org_name"], "primary_name", i))
        if row.get("other_names"):
            for alt in row["other_names"].split("||"):
                if alt.strip():
                    all_entries.append((normalize_name(alt), alt, "other_name", i))

    all_norm_names = [e[0] for e in all_entries]

    # City index: city_upper -> list of all_entries indices
    city_entries = {}
    for j, (_, _, _, row_idx) in enumerate(all_entries):
        city = (rows[row_idx].get("practice_city") or "").strip().upper()
        if city:
            city_entries.setdefault(city, []).append(j)

    # ZIP index: zip5 -> list of all_entries indices
    zip_entries = {}
    for j, (_, _, _, row_idx) in enumerate(all_entries):
        zip5 = (rows[row_idx].get("practice_zip") or "").strip()[:5]
        if zip5:
            zip_entries.setdefault(zip5, []).append(j)

    return {
        "rows":           rows,
        "all_entries":    all_entries,
        "all_norm_names": all_norm_names,
        "city_entries":   city_entries,
        "zip_entries":    zip_entries,
    }


def get_state_cache(state_abbr):
    with _state_cache_lock:
        if state_abbr not in _state_cache:
            _state_cache[state_abbr] = load_state_cache(state_abbr)
        return _state_cache[state_abbr]


# ---------------------------------------------------------------------------
# Scoring and confidence
# ---------------------------------------------------------------------------

def score_against_entries(norm_query, entry_indices, all_entries, all_norm_names, rows):
    """
    Run rapidfuzz against a subset of entries (by index list).
    Returns list of (score, source, row) sorted descending, deduped by NPI.
    """
    if entry_indices is None:
        # Use all entries
        names   = all_norm_names
        entries = all_entries
    else:
        names   = [all_norm_names[j] for j in entry_indices]
        entries = [all_entries[j]    for j in entry_indices]

    if not names:
        return []

    results = fuzz_process.extract(
        norm_query, names,
        scorer=fuzz.token_sort_ratio,
        limit=TOP_N * 4,
        score_cutoff=30,
    )

    seen = {}
    for _, score_int, idx in results:
        norm_name, orig_name, source, row_idx = entries[idx]
        row   = rows[row_idx]
        score = score_int / 100.0
        npi   = row["npi"]
        if npi not in seen or score > seen[npi][0]:
            seen[npi] = (score, source, row)

    return sorted(seen.values(), key=lambda x: x[0], reverse=True)[:TOP_N]


# ---------------------------------------------------------------------------
# Per-center matching
# ---------------------------------------------------------------------------

def process_center(center):
    hhl_id    = center["id"]
    hhl_name  = center["name"].strip()
    hhl_state = center["location"].strip()
    hhl_city  = center.get("city", "").strip()
    hhl_zip   = center.get("zipcode", "").strip()
    hhl_url   = center.get("url", "").strip()

    base = {
        "hhl_id": hhl_id, "hhl_name": hhl_name, "hhl_state": hhl_state,
        "hhl_city": hhl_city, "hhl_zip": hhl_zip, "hhl_url": hhl_url,
    }
    empty = {
        "nppes_npi": "", "nppes_name": "", "nppes_address": "",
        "nppes_city": "", "nppes_state": "", "nppes_zip": "",
        "nppes_phone": "", "nppes_parent_org": "",
    }

    state_abbr = STATE_ABBR.get(hhl_state)
    if not state_abbr:
        return [{**base, "rank": "", "confidence": "NO_STATE", "match_score": "",
                 "match_source": "", "signals_matched": "", **empty, "action": "NEEDS_REVIEW"}]

    cache         = get_state_cache(state_abbr)
    norm_query    = normalize_name(hhl_name)
    zip5          = hhl_zip.strip()[:5] if hhl_zip else ""
    city_upper    = hhl_city.strip().upper() if hhl_city else ""

    # Pick the narrowest available location bucket
    if zip5 and zip5 in cache["zip_entries"]:
        entry_indices = cache["zip_entries"][zip5]
    elif city_upper and city_upper in cache["city_entries"]:
        entry_indices = cache["city_entries"][city_upper]
    else:
        entry_indices = None  # fall back to full state

    top = score_against_entries(
        norm_query, entry_indices,
        cache["all_entries"], cache["all_norm_names"], cache["rows"],
    )

    # If location-filtered search returned nothing, fall back to full state
    if not top and entry_indices is not None:
        top = score_against_entries(
            norm_query, None,
            cache["all_entries"], cache["all_norm_names"], cache["rows"],
        )

    if not top:
        return [{**base, "rank": "", "confidence": "NO_MATCH", "match_score": "",
                 "match_source": "", "signals_matched": "", **empty, "action": "NEEDS_REVIEW"}]

    is_unique = len(top) < 2 or (top[0][0] - top[1][0] >= 0.15)
    zip_missing = not bool(zip5)

    out_rows = []
    for rank, (score, source, row) in enumerate(top, 1):
        zip5_nppes = (row.get("practice_zip") or "").strip()[:5]
        city_match = bool(
            hhl_city and row.get("practice_city") and
            hhl_city.strip().upper() == row["practice_city"].strip().upper()
        )
        zip_match = bool(zip5 and zip5_nppes and zip5 == zip5_nppes)
        composite, signals = center_composite(
            score, city_match, zip_match, zip_missing,
            is_unique=(rank == 1 and is_unique),
        )
        confidence = confidence_from_score(composite)
        out_rows.append({
            **base,
            "rank":             rank,
            "confidence":       confidence,
            "match_score":      f"{composite:.3f}",
            "match_source":     source,
            "signals_matched":  "|".join(signals),
            "nppes_npi":        row["npi"],
            "nppes_name":       row["org_name"]          or "",
            "nppes_address":    row["practice_address1"] or "",
            "nppes_city":       row["practice_city"]     or "",
            "nppes_state":      row["practice_state"]    or "",
            "nppes_zip":        row["practice_zip"]      or "",
            "nppes_phone":      row["practice_phone"]    or "",
            "nppes_parent_org": row["parent_org_name"]   or "",
            "action":           "REVIEW",
        })
    return out_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run: python3 build_local_db.py")
        return

    print("Loading HHL medical centers...")
    with open(INPUT_CSV, encoding="utf-8") as f:
        centers = list(csv.DictReader(f))

    total = len(centers)
    print(f"Matching {total} centers using {WORKERS} workers...\n")

    completed = 0
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for result_rows in executor.map(process_center, centers):
                writer.writerows(result_rows)
                completed += 1
                if completed % 200 == 0:
                    print(f"  {completed}/{total} processed...", flush=True)

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO_MATCH": 0, "NO_STATE": 0}
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["rank"] in ("", "1"):
                c = row["confidence"]
                if c in counts:
                    counts[c] += 1

    print(f"\nDone — {total} centers processed.")
    for label, n in counts.items():
        print(f"  {label:10}: {n}")
    print(f"\nHIGH = name>=0.85 + all available location signals (city/zip) confirmed")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
