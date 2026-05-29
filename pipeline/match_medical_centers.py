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
from scoring import (
    center_composite, zip_compare,
    assess_candidate_strength, assess_selection_confidence,
)

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(BASE_DIR, "data")
DB_PATH    = os.path.join(DATA_DIR, "nppes_local.db")
INPUT_CSV  = os.path.join(DATA_DIR, "campaign_medicalcenter.csv")
OUTPUT_CSV = os.path.join(DATA_DIR, "medical_center_matches.csv")

WORKERS = 12
TOP_N   = 25

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
    "candidate_strength", "name_score", "city_score", "zip_score",
    "margin", "confidence_flags", "candidate_count", "sibling_count",
    "nppes_npi", "nppes_name", "nppes_address", "nppes_city",
    "nppes_state", "nppes_zip", "nppes_phone", "nppes_parent_org", "action",
]

# Names that are known HHL placeholders — not real organization names.
# Matched case-insensitively against the stripped HHL name.
GENERIC_NAME_BLOCKLIST = {
    "primary physician",
    "private physician",
    "private office",
    "private practice",
}

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
    r",?\s*\binc\.?\b",  r",?\s*\bllc\.?\b",  r",?\s*\bcorp\.?\b",
    r",?\s*\bltd\.?\b",  r",?\s*\blp\.?\b",   r",?\s*\bpc\.?\b",
    r",?\s*\bpa\.?\b",   r",?\s*\bpllc\.?\b", r",?\s*\bpllp\.?\b",
    r",?\s*\bllp\.?\b",  r",?\s*\bnpc\.?\b",  r",?\s*\bplc\.?\b",
]
_ABBREV_RE      = [(re.compile(p, re.IGNORECASE), r) for p, r in _ABBREV_PATTERNS]
_SUFFIX_RE      = [re.compile(p, re.IGNORECASE)      for p    in _STRIP_SUFFIXES]
_THE_PREFIX_RE  = re.compile(r"^the\s+", re.IGNORECASE)
_POSSESSIVE_RE  = re.compile(r"'s\b", re.IGNORECASE)
_CONNECTOR_RE   = re.compile(r"\band\b", re.IGNORECASE)
_PUNCT_RE       = re.compile(r"[^\w\s]")
_SPACES_RE      = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    for pat, repl in _ABBREV_RE:
        n = pat.sub(repl, n)
    for pat in _SUFFIX_RE:
        n = pat.sub("", n)
    n = _THE_PREFIX_RE.sub("", n)
    n = _POSSESSIVE_RE.sub("s", n)
    n = _PUNCT_RE.sub(" ", n)
    n = _SPACES_RE.sub(" ", n).strip()
    return n


# Generic medical descriptor words that inflate false-positive scores.
# Stripped from both query and candidate names before fuzzy comparison only —
# the full normalized name is still used for indexing and display.
_SCORE_STOP_RE = re.compile(
    r"\bprimary\s+(?:care|physicians?|medicine|med)\b"
    r"|\bphysicians?\b"
    r"|\bassociates?\b",
    re.IGNORECASE,
)

NAME_QUALITY_THRESHOLD = 0.85  # if best city-filtered name score < this, also search state-wide


def scoring_name(normalized: str) -> str:
    """Strip generic descriptor words before fuzzy comparison."""
    n = _SCORE_STOP_RE.sub(" ", normalized)
    return _SPACES_RE.sub(" ", n).strip()


def connector_name(normalized: str) -> str:
    """Strip connector words ('and') so '&' and 'and' variants score identically."""
    n = _CONNECTOR_RE.sub(" ", normalized)
    return _SPACES_RE.sub(" ", n).strip()


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

    all_norm_names      = [e[0] for e in all_entries]
    all_scoring_names   = [scoring_name(e[0])   for e in all_entries]
    all_connector_names = [connector_name(e[0]) for e in all_entries]

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
        "rows":              rows,
        "all_entries":       all_entries,
        "all_norm_names":      all_norm_names,
        "all_scoring_names":   all_scoring_names,
        "all_connector_names": all_connector_names,
        "city_entries":      city_entries,
        "zip_entries":       zip_entries,
    }


def get_state_cache(state_abbr):
    with _state_cache_lock:
        if state_abbr not in _state_cache:
            _state_cache[state_abbr] = load_state_cache(state_abbr)
        return _state_cache[state_abbr]


# ---------------------------------------------------------------------------
# Scoring and confidence
# ---------------------------------------------------------------------------

def score_against_entries(norm_query, entry_indices, all_entries, all_norm_names, all_scoring_names, all_connector_names, rows):
    """
    Runs three fuzzy passes and takes the MAX score for each NPI:
      Pass 1 — full normalized names
      Pass 2 — stop-word-stripped names (catches keyword matches)
      Pass 3 — connector-stripped names ('and' removed so '&' and 'and' score identically)
    Returns list of (score, source, row, keyword_match) sorted descending, deduped by NPI.
    """
    if entry_indices is None:
        full_names      = all_norm_names
        score_names     = all_scoring_names
        conn_names      = all_connector_names
        entries         = all_entries
    else:
        full_names      = [all_norm_names[j]      for j in entry_indices]
        score_names     = [all_scoring_names[j]   for j in entry_indices]
        conn_names      = [all_connector_names[j] for j in entry_indices]
        entries         = [all_entries[j]         for j in entry_indices]

    if not full_names:
        return []

    query_full      = norm_query
    query_stripped  = scoring_name(norm_query)
    query_connector = connector_name(norm_query)

    seen = {}  # npi -> (score, source, row, keyword_match)

    # Pass 1: full normalized names
    for _, score_int, idx in fuzz_process.extract(
        query_full, full_names,
        scorer=fuzz.token_sort_ratio,
        limit=TOP_N * 4,
        score_cutoff=30,
    ):
        _, _, source, row_idx = entries[idx]
        row = rows[row_idx]
        score = score_int / 100.0
        npi = row["npi"]
        if npi not in seen or score > seen[npi][0]:
            seen[npi] = (score, source, row, False)

    # Pass 2: stop-word-stripped names — takes over only when score is higher
    for _, score_int, idx in fuzz_process.extract(
        query_stripped, score_names,
        scorer=fuzz.token_sort_ratio,
        limit=TOP_N * 4,
        score_cutoff=30,
    ):
        _, _, source, row_idx = entries[idx]
        row = rows[row_idx]
        score = score_int / 100.0
        npi = row["npi"]
        if npi not in seen or score > seen[npi][0]:
            seen[npi] = (score, source, row, True)

    # Pass 3: connector-stripped names — '&' and 'and' both removed, scores identically
    for _, score_int, idx in fuzz_process.extract(
        query_connector, conn_names,
        scorer=fuzz.token_sort_ratio,
        limit=TOP_N * 4,
        score_cutoff=30,
    ):
        _, _, source, row_idx = entries[idx]
        row = rows[row_idx]
        score = score_int / 100.0
        npi = row["npi"]
        if npi not in seen or score > seen[npi][0]:
            seen[npi] = (score, source, row, False)

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

    if hhl_name.strip().lower() in GENERIC_NAME_BLOCKLIST:
        return [{**base, "rank": "", "confidence": "NOT_MATCHABLE", "match_score": "",
                 "match_source": "", "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "", "zip_score": "",
                 "margin": "", "confidence_flags": "generic_placeholder", "candidate_count": "", "sibling_count": "",
                 **empty, "action": "NEEDS_REVIEW"}]

    state_abbr = STATE_ABBR.get(hhl_state)
    if not state_abbr:
        return [{**base, "rank": "", "confidence": "NO_STATE", "match_score": "",
                 "match_source": "", "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "", "zip_score": "",
                 "margin": "", "confidence_flags": "", "candidate_count": "", "sibling_count": "",
                 **empty, "action": "NEEDS_REVIEW"}]

    cache         = get_state_cache(state_abbr)
    norm_query    = normalize_name(hhl_name)
    zip5          = hhl_zip.strip()[:5] if hhl_zip else ""
    # Split slash-separated city values (e.g. "Westfield/West Springfield")
    city_parts    = [p.strip().upper() for p in hhl_city.split("/") if p.strip()] if hhl_city else []

    # Pick the narrowest available location bucket.
    # Slash cities: union ZIP bucket + all city-part buckets so both offices are candidates.
    if city_parts and len(city_parts) > 1:
        combined = set()
        if zip5 and zip5 in cache["zip_entries"]:
            combined.update(cache["zip_entries"][zip5])
        for part in city_parts:
            if part in cache["city_entries"]:
                combined.update(cache["city_entries"][part])
        entry_indices = combined if combined else None
    elif zip5 and zip5 in cache["zip_entries"]:
        entry_indices = cache["zip_entries"][zip5]
    elif city_parts:
        combined = set()
        for part in city_parts:
            if part in cache["city_entries"]:
                combined.update(cache["city_entries"][part])
        entry_indices = combined if combined else None
    else:
        entry_indices = None  # fall back to full state

    top = score_against_entries(
        norm_query, entry_indices,
        cache["all_entries"], cache["all_norm_names"], cache["all_scoring_names"], cache["all_connector_names"], cache["rows"],
    )

    # Fallback 1: location search returned nothing → try full state
    if not top and entry_indices is not None:
        top = score_against_entries(
            norm_query, None,
            cache["all_entries"], cache["all_norm_names"], cache["all_scoring_names"], cache["all_connector_names"], cache["rows"],
        )
    # Fallback 2: best name score is poor (org probably in a different city than HHL recorded)
    # → merge location results with state-wide results so out-of-city siblings surface
    elif top and entry_indices is not None and top[0][0] < NAME_QUALITY_THRESHOLD:
        state_top = score_against_entries(
            norm_query, None,
            cache["all_entries"], cache["all_norm_names"], cache["all_scoring_names"], cache["all_connector_names"], cache["rows"],
        )
        seen = {}
        for score, source, row, kw in top + state_top:
            npi = row["npi"]
            if npi not in seen or score > seen[npi][0]:
                seen[npi] = (score, source, row, kw)
        top = sorted(seen.values(), key=lambda x: x[0], reverse=True)[:TOP_N]

    if not top:
        return [{**base, "rank": "", "confidence": "NO_MATCH", "match_score": "",
                 "match_source": "", "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "", "zip_score": "",
                 "margin": "", "confidence_flags": "", "candidate_count": "", "sibling_count": "",
                 **empty, "action": "NEEDS_REVIEW"}]

    city_missing = not bool(hhl_city.strip() if hhl_city else "")
    zip_missing = not bool(zip5)

    # --- Pass 1: base composites ---
    intermediate = []
    for name_score, source, row, keyword_match in top:
        city_match = bool(
            city_parts and row.get("practice_city") and
            any(part == row["practice_city"].strip().upper() for part in city_parts)
        )
        zip_match, zip_match_level = zip_compare(hhl_zip, row.get("practice_zip"))
        city_conflict = (
            bool(hhl_city) and bool(row.get("practice_city")) and
            not city_match and not zip_match
        )
        base_c, base_s, base_fs = center_composite(
            name_score, city_match, zip_match, zip_missing,
            is_unique=False,
            zip_match_level=zip_match_level,
            city_missing=city_missing,
        )
        intermediate.append({
            "name_score":        name_score,
            "source":            source,
            "keyword_match":     keyword_match,
            "row":               row,
            "city_match":        city_match,
            "zip_match":         zip_match,
            "zip_match_level":   zip_match_level,
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
        fc, fs, ffs = center_composite(
            c0["name_score"], c0["city_match"], c0["zip_match"], zip_missing,
            is_unique=True,
            zip_match_level=c0["zip_match_level"],
            city_missing=city_missing,
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
            "rank":               rank,
            "confidence":         confidence,
            "match_score":        f"{c['final_composite']:.3f}",
            "match_source":       "keyword_match" if c["keyword_match"] else c["source"],
            "signals_matched":    "|".join(c["final_signals"]),
            "candidate_strength": strength,
            "name_score":         f"{ffs['name']:.3f}",
            "city_score":         f"{ffs['city']:.3f}" if ffs["city"] is not None else "",
            "zip_score":          f"{ffs['zip']:.3f}" if ffs["zip"] is not None else "",
            "margin":             f"{margin:.3f}" if margin is not None else "",
            "confidence_flags":   "|".join(conf_flags),
            "candidate_count":    candidate_count,
            "nppes_npi":          c["row"]["npi"],
            "nppes_name":         c["row"]["org_name"]          or "",
            "nppes_address":      c["row"]["practice_address1"] or "",
            "nppes_city":         c["row"]["practice_city"]     or "",
            "nppes_state":        c["row"]["practice_state"]    or "",
            "nppes_zip":          c["row"]["practice_zip"]      or "",
            "nppes_phone":        c["row"]["practice_phone"]    or "",
            "nppes_parent_org":   c["row"]["parent_org_name"]   or "",
            "action":             "REVIEW",
        })
    # Sibling-location detection: two+ candidates with name_score >= 0.90 but
    # different NPPES cities — same org at multiple locations.
    sibling_count = 0
    high_name = [r for r in out_rows if r.get("name_score") and float(r["name_score"]) >= 0.97]
    if len(high_name) >= 2:
        sibling_cities = {r["nppes_city"].strip().upper() for r in high_name if r.get("nppes_city")}
        if len(sibling_cities) > 1:
            for r in out_rows:
                if r.get("name_score") and float(r["name_score"]) >= 0.90:
                    existing = r.get("confidence_flags", "")
                    r["confidence_flags"] = (existing + "|sibling_location").lstrip("|") if existing else "sibling_location"
            sibling_count = sum(1 for r in out_rows if "sibling_location" in r.get("confidence_flags", ""))

    for r in out_rows:
        r["sibling_count"] = sibling_count

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

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO_MATCH": 0, "NO_STATE": 0, "NOT_MATCHABLE": 0}
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
