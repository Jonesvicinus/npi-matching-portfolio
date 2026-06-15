#!/usr/bin/env python3
"""
Phase 2 professional matching: center-anchor approach.

For professionals whose center has a confirmed NPI (APPROVED in review_decisions.db
or HIGH-confidence in medical_center_matches.csv), narrows the NPPES candidate pool
to providers registered at that center's specific city/ZIP instead of searching the
whole state. A physician named "John Smith" in California has ~500 state candidates
but typically 2-5 at a specific hospital ZIP — dramatically reducing false positives.

Falls back to full-state phase-1 logic for any professional whose center is not yet
confirmed.  signals_matched includes "anchor" for phase-2 matches so reviewers can
see which method was used.

Prerequisites:
    python3 match_medical_centers.py          (must have run first)
    python3 match_medical_professionals.py    (phase 1, used as state-search fallback)

Run:
    python3 match_professionals_phase2.py

Output: data/medical_professional_matches_phase2.csv
  The review site auto-detects this file and uses it instead of phase-1 when present.
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

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR       = os.path.join(BASE_DIR, "data")
DB_PATH        = os.path.join(DATA_DIR, "nppes_local.db")
PROF_CSV       = os.path.join(DATA_DIR, "campaign_medicalprofessional_enriched.csv")
CENTER_CSV     = os.path.join(DATA_DIR, "campaign_medicalcenter.csv")
CENTER_MATCHES = os.path.join(DATA_DIR, "medical_center_matches.csv")
DECISIONS_DB   = os.path.join(DATA_DIR, "review_decisions.db")
OUTPUT_CSV     = os.path.join(DATA_DIR, "medical_professional_matches_phase2.csv")

WORKERS = 12
TOP_N   = 5

# National name-anchored fallback thresholds.
# A provider's NPPES practice_state often isn't where HHL lists them (relocations,
# multi-state systems like CommonSpirit, providers on a state border). The single-
# state search misses them entirely. When no strong same-name candidate turns up
# in-state, we search the full registry by surname and inject only strong same-name
# matches — surfacing the real person without adding same-surname noise.
# Thresholds are on the combined name score (last*0.6 + first*0.4). Because every
# candidate already shares the surname, that 0.6 floor means a merely similar first
# name still scores ~0.85 — so the gates sit higher, keyed to first-name strength:
#   trigger 0.92  ⇒ fire only when no in-state candidate's first name is strong
#                   (for an exact surname, first-name similarity < 0.80)
#   merge   0.95  ⇒ inject only strong same-name national matches
#                   (first-name similarity >= ~0.875), never same-surname noise
NATIONAL_TRIGGER_THRESHOLD = 0.92   # run national search when best in-state name < this
NATIONAL_MERGE_THRESHOLD   = 0.95   # only merge national candidates with name_score >= this

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

# ---------------------------------------------------------------------------
# Center confirmation lookup
# ---------------------------------------------------------------------------

_center_approved = {}   # center_id -> nppes_npi  (from decisions DB)
_center_high     = {}   # center_id -> nppes_npi  (from HIGH matches, fallback)


def load_center_confirmations():
    if os.path.exists(DECISIONS_DB):
        conn = sqlite3.connect(DECISIONS_DB)
        for row in conn.execute(
            "SELECT hhl_id, nppes_npi FROM decisions WHERE hhl_type='center' AND decision='APPROVED'"
        ):
            if row[1]:
                _center_approved[row[0]] = row[1]
        conn.close()

    if os.path.exists(CENTER_MATCHES):
        with open(CENTER_MATCHES, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("rank") == "1" and row.get("confidence") == "HIGH":
                    if row.get("nppes_npi"):
                        _center_high[row["hhl_id"]] = row["nppes_npi"]


def get_confirmed_npi(center_id):
    if center_id in _center_approved:
        return _center_approved[center_id], "approved"
    if center_id in _center_high:
        return _center_high[center_id], "inferred"
    return None, None


# ---------------------------------------------------------------------------
# Center location lookup (one DB hit per unique NPI)
# ---------------------------------------------------------------------------

_center_loc_cache      = {}
_center_loc_cache_lock = threading.Lock()


def get_center_location(npi):
    with _center_loc_cache_lock:
        if npi in _center_loc_cache:
            return _center_loc_cache[npi]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT practice_state, practice_city, practice_zip FROM providers WHERE npi = ?",
            (npi,)
        ).fetchone()
        conn.close()
        result = dict(row) if row else None
        _center_loc_cache[npi] = result
        return result


# ---------------------------------------------------------------------------
# Location-scoped individual provider cache
# Key: (state, zip5)  — shared across all professionals at the same center
# ---------------------------------------------------------------------------

_loc_cache      = {}
_loc_cache_lock = threading.Lock()


def _query_location(conn, state, zip5, city):
    sql = """
        SELECT p.npi, p.first_name, p.last_name, p.credential,
               p.practice_address1, p.practice_city, p.practice_state,
               p.practice_zip, p.practice_phone,
               t.taxonomy_code
        FROM providers p
        LEFT JOIN taxonomies t ON t.npi = p.npi AND t.is_primary = 1
        WHERE p.entity_type = 1
          AND p.practice_state = ?
          AND p.deactivation_date IS NULL
          AND ({loc_clause})
    """
    if zip5 and city:
        rows = conn.execute(
            sql.format(loc_clause="p.practice_zip LIKE ? OR p.practice_city = ?"),
            (state, zip5 + "%", city.strip().upper()),
        ).fetchall()
    elif zip5:
        rows = conn.execute(
            sql.format(loc_clause="p.practice_zip LIKE ?"),
            (state, zip5 + "%"),
        ).fetchall()
    elif city:
        rows = conn.execute(
            sql.format(loc_clause="p.practice_city = ?"),
            (state, city.strip().upper()),
        ).fetchall()
    else:
        rows = []
    return [dict(r) for r in rows]


def get_individuals_at_location(state, zip5, city):
    key = (state, zip5 or "", (city or "").strip().upper())
    with _loc_cache_lock:
        if key in _loc_cache:
            return _loc_cache[key]

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = _query_location(conn, state, zip5, city)
        conn.close()

        last_names = [(r["last_name"] or "").lower() for r in rows]
        _loc_cache[key] = (rows, last_names)
        return rows, last_names


# ---------------------------------------------------------------------------
# State-level fallback cache (same as phase 1)
# ---------------------------------------------------------------------------

_state_cache      = {}
_state_cache_lock = threading.Lock()


def get_state_individuals(state_abbr):
    with _state_cache_lock:
        if state_abbr not in _state_cache:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("""
                SELECT p.npi, p.first_name, p.last_name, p.credential,
                       p.practice_address1, p.practice_city, p.practice_state,
                       p.practice_zip, p.practice_phone,
                       t.taxonomy_code
                FROM providers p
                LEFT JOIN taxonomies t ON t.npi = p.npi AND t.is_primary = 1
                WHERE p.entity_type = 1
                  AND p.practice_state = ?
                  AND p.deactivation_date IS NULL
            """, (state_abbr,)).fetchall()]
            conn.close()
            _state_cache[state_abbr] = (rows, [(r["last_name"] or "").lower() for r in rows])
        return _state_cache[state_abbr]


# ---------------------------------------------------------------------------
# National surname cache (full registry, all states) — for the name-anchored
# fallback. Keyed by normalized surname; the result set for one surname is small
# and the query is backed by idx_providers_lastname.
# ---------------------------------------------------------------------------

_national_cache      = {}
_national_cache_lock = threading.Lock()


def get_individuals_by_last_name_national(last_name):
    """All active individual providers nationwide with this exact (normalized) surname.

    First-name similarity + credential/taxonomy disambiguate within the result;
    exact surname keeps the set small and the query index-backed.
    """
    key = (last_name or "").strip().upper()
    if not key:
        return [], []
    with _national_cache_lock:
        if key in _national_cache:
            return _national_cache[key]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("""
            SELECT p.npi, p.first_name, p.last_name, p.credential,
                   p.practice_address1, p.practice_city, p.practice_state,
                   p.practice_zip, p.practice_phone,
                   t.taxonomy_code
            FROM providers p
            LEFT JOIN taxonomies t ON t.npi = p.npi AND t.is_primary = 1
            WHERE p.entity_type = 1
              AND p.last_name = ?
              AND p.deactivation_date IS NULL
        """, (key,)).fetchall()]
        conn.close()
        last_names = [(r["last_name"] or "").lower() for r in rows]
        _national_cache[key] = (rows, last_names)
        return rows, last_names


# Email domain → center_id mapping (built in main())
_domain_center_map = {}


def _domain_from_email(email):
    if "@" in email:
        return email.split("@", 1)[1].strip().lower()
    return ""


# ---------------------------------------------------------------------------
# Scoring helpers (identical to phase 1)
# ---------------------------------------------------------------------------

def taxonomy_matches(row, prefixes):
    if not prefixes:
        return True
    code = row.get("taxonomy_code") or ""
    return any(code.startswith(p) for p in prefixes)


def credential_aligns(prof_type, credential):
    if not credential:
        return False
    keywords = CREDENTIAL_KEYWORDS.get(prof_type, [])
    return any(kw in credential.upper() for kw in keywords)


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



def rank_candidates(all_rows, all_last_names, last_name, name_expansions, prefixes):
    """rapidfuzz pre-filter on last name, then full score. Returns top TOP_N."""
    last_results = fuzz_process.extract(
        last_name.lower(), all_last_names,
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
    return scored[:TOP_N]


def rank_national_candidates(rows, last_name, name_expansions):
    """Rank exact-surname national rows by first name — the only discriminating axis.

    rank_candidates() pre-filters by surname with a 200-row cutoff, which is wrong
    here: every national row already shares the surname, so that cutoff would drop
    the true first-name match (e.g. the one Shundrika Scott among 9k Scotts) before
    it is ever scored. Here we score every row by first-name similarity and keep the
    best TOP_N.
    """
    scored = []
    for row in rows:
        best_first = max(
            (fuzz.token_sort_ratio(exp.lower(), (row["first_name"] or "").lower()) / 100.0
             for exp in name_expansions),
            default=0.0,
        )
        if best_first < 0.40:
            continue
        scored.append((score_individual(row, last_name, name_expansions), row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:TOP_N]


# ---------------------------------------------------------------------------
# Per-professional matching
# ---------------------------------------------------------------------------

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
    prefixes        = TAXONOMY_PREFIXES.get(prof_type, [])

    # --- Phase 2: try center-anchor search first ---
    anchored      = False
    anchor_type   = None
    confirmed_npi, anchor_type = get_confirmed_npi(center_id)
    anchor_city   = center_city

    # Email domain → upgrade inferred anchor to approved when domain confirms center.
    email_domain = _domain_from_email(email)
    if (email_domain and anchor_type == "inferred" and
            _domain_center_map.get(email_domain) == center_id):
        anchor_type = "approved"

    anchor_top = []
    if confirmed_npi:
        loc = get_center_location(confirmed_npi)
        if loc:
            zip5        = (loc.get("practice_zip") or "").strip()[:5]
            anchor_city = (loc.get("practice_city") or center_city or "").strip()
            loc_state   = loc.get("practice_state") or state_abbr
            if zip5 or anchor_city:
                loc_rows, loc_last_names = get_individuals_at_location(
                    loc_state, zip5, anchor_city
                )
                if loc_rows:
                    anchor_top = rank_candidates(loc_rows, loc_last_names, last_name, name_expansions, prefixes)

    # Use anchor results when the best name_score is strong enough (>= 0.80).
    # Below that threshold the anchor location may be wrong (person may have moved),
    # so fall through to state-wide search to avoid missing better candidates.
    anchor_best_name = anchor_top[0][0] if anchor_top else 0.0
    if anchor_top and anchor_best_name >= 0.80:
        top      = anchor_top
        anchored = True
    else:
        # Fallback: full state search — also run when anchor results are weak.
        all_rows, all_last_names = get_state_individuals(state_abbr)
        state_top = rank_candidates(all_rows, all_last_names, last_name, name_expansions, prefixes)

        if anchor_top and anchor_best_name > 0:
            # Merge: combine anchor and state candidates, deduplicate by NPI, keep TOP_N best.
            seen_npis = set()
            merged = []
            for score, row in anchor_top + state_top:
                npi = row["npi"]
                if npi not in seen_npis:
                    seen_npis.add(npi)
                    merged.append((score, row))
            merged.sort(key=lambda x: x[0], reverse=True)
            top = merged[:TOP_N]
            anchored = True  # partial anchor — signals still included
        else:
            top = state_top
            anchor_city = center_city

    # --- National name-anchored fallback ---
    # If no strong same-name candidate turned up in-state/at-anchor, search the
    # full registry by surname to catch providers registered in another state.
    # Only strong same-name national matches (>= NATIONAL_MERGE_THRESHOLD) are
    # injected, so we surface the real person without adding same-surname noise.
    national_npis = set()
    best_name = top[0][0] if top else 0.0
    if best_name < NATIONAL_TRIGGER_THRESHOLD:
        nat_rows, _nat_last = get_individuals_by_last_name_national(last_name)
        if nat_rows:
            nat_top = rank_national_candidates(nat_rows, last_name, name_expansions)
            strong  = [(s, row) for s, row in nat_top if s >= NATIONAL_MERGE_THRESHOLD]
            # Only trust a national match when it is nationally UNAMBIGUOUS — exactly
            # one strong same-name provider. Common names ("Nancy Arnold") have many
            # namesakes across states that cannot be told apart by name, so surfacing
            # one as a confident match would be a guess. Distinctive names (the lone
            # "Shundrika Scott") have exactly one and are the real win.
            if len(strong) == 1:
                seen = {row["npi"] for _, row in top}
                if strong[0][1]["npi"] not in seen:
                    national_npis = {strong[0][1]["npi"]}
                    merged = top + strong
                    merged.sort(key=lambda x: x[0], reverse=True)
                    top = merged[:TOP_N]
                    # A national candidate may be trimmed out by TOP_N — keep only
                    # those that survived into the final set.
                    national_npis &= {row["npi"] for _, row in top}

    if not top:
        return [{**base, "rank": "", "confidence": "NO_MATCH", "match_score": "",
                 "signals_matched": "",
                 "candidate_strength": "", "name_score": "", "city_score": "",
                 "credential_score": "", "taxonomy_score": "", "phone_score": "",
                 "margin": "", "confidence_flags": "", "candidate_count": "",
                 **empty, "action": "NEEDS_REVIEW"}]

    city_missing = not bool(anchor_city)

    # --- Pass 1: base composites ---
    intermediate = []
    for name_score, row in top:
        city_match = bool(
            anchor_city and row.get("practice_city") and
            anchor_city.strip().upper() == row["practice_city"].strip().upper()
        )
        cred_match  = credential_aligns(prof_type, row.get("credential", ""))
        tax_match   = taxonomy_matches(row, prefixes)
        phone_match, phone_score = compute_phone_match(hhl_phone, row.get("practice_phone", ""))

        if anchored and anchor_type == "approved" and row["npi"] not in national_npis:
            city_val      = 1.0
            city_conflict = False   # anchor_approved bypasses conflict
        else:
            city_val      = 1.0 if city_match else 0.0
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

    # Append anchor signal to every candidate's final_signals AFTER composite finalized.
    # National-sourced candidates are out-of-area, so they never inherit the anchor
    # signal — they carry "national" instead.
    if anchored:
        anchor_signal = "anchor_approved" if anchor_type == "approved" else "anchor_inferred"
        for c in intermediate:
            if c["row"]["npi"] not in national_npis:
                c["final_signals"] = c["final_signals"] + [anchor_signal]
    for c in intermediate:
        if c["row"]["npi"] in national_npis:
            c["final_signals"] = c["final_signals"] + ["national"]

    # --- Pass 2: build output rows ---
    out_rows = []
    for rank, c in enumerate(intermediate, 1):
        ffs = c["final_field_scores"]
        strength = assess_candidate_strength(c["final_composite"])
        confidence, conf_flags = assess_selection_confidence(
            c["final_composite"], c["city_conflict"], margin,
            name_score=ffs["name"],
        )
        # A national (out-of-state) pick has no city to verify against, so when
        # neither credential nor taxonomy corroborates it the name is the ONLY
        # evidence — not enough for HIGH (a coincidental namesake could be approved).
        # Cap such picks at MEDIUM and flag why. Corroborated national picks (e.g.
        # Shundrika Scott: LCSW + social-worker taxonomy) keep their tier.
        if (c["row"]["npi"] in national_npis and confidence == "HIGH"
                and not (c["cred_match"] or c["tax_match"])):
            confidence = "MEDIUM"
            conf_flags = conf_flags + ["national_name_only"]
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run: python3 build_local_db.py")
        return
    if not os.path.exists(CENTER_MATCHES):
        print(f"ERROR: {CENTER_MATCHES} not found. Run: python3 match_medical_centers.py")
        return

    # Ensure the surname index exists — the national fallback's per-surname query
    # is a full table scan (~0.6s) without it, but ~10ms with it. Idempotent;
    # build_local_db.py also creates it, this covers DBs built before that change.
    print("Ensuring surname index for national fallback...")
    _conn = sqlite3.connect(DB_PATH)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_providers_lastname ON providers (last_name, entity_type)"
    )
    _conn.close()

    print("Loading center confirmations...")
    load_center_confirmations()
    anchored_centers = len(_center_approved) + len(_center_high)
    print(f"  {len(_center_approved)} APPROVED + {len(_center_high)} HIGH = {anchored_centers} confirmed centers")

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

    # Build email domain → center_id map (majority-vote, min 2 professionals per domain).
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
        if counter.most_common(1)[0][1] >= 2
    }
    print(f"  {len(_domain_center_map)} email domains mapped to centers")

    total = len(professionals)
    print(f"Matching {total} professionals using {WORKERS} workers...\n")

    args      = [(p, center_lookup) for p in professionals]
    completed = 0
    phase2_count = 0

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for result_rows in executor.map(process_professional, args):
                writer.writerows(result_rows)
                if result_rows and (
                    "anchor_approved" in (result_rows[0].get("signals_matched") or "") or
                    "anchor_inferred" in (result_rows[0].get("signals_matched") or "")
                ):
                    phase2_count += 1
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
    print(f"  Phase 2 (center-anchored): {phase2_count}")
    print(f"  Phase 1 fallback:          {total - phase2_count - counts.get('SKIPPED', 0)}")
    print()
    for label, n in counts.items():
        print(f"  {label:10}: {n}")
    print(f"\nAnchor signal: ✓ anchor = searched within confirmed center's city/ZIP")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
