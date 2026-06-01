#!/usr/bin/env python3
"""
Local review site + enhanced profile pages for NPI matching.

Run:
    cd review_site
    pip install flask
    python app.py

Then open: http://localhost:5000
"""

import csv
import io
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template, request, url_for, Response
from markupsafe import Markup, escape

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(BASE_DIR, "data")
NPPES_DB     = os.path.join(DATA_DIR, "nppes_local.db")
DECISIONS_DB = os.path.join(DATA_DIR, "review_decisions.db")
CENTER_CSV   = os.path.join(DATA_DIR, "medical_center_matches.csv")
HHL_CENTERS  = os.path.join(DATA_DIR, "campaign_medicalcenter.csv")
HHL_PROFS    = os.path.join(DATA_DIR, "campaign_medicalprofessional_enriched.csv")
CONFIRMED_CSV  = os.path.join(DATA_DIR, "hhl_confirmed.csv")
_PROF_CSV_P2   = os.path.join(DATA_DIR, "medical_professional_matches_phase2.csv")
_PROF_CSV_P1   = os.path.join(DATA_DIR, "medical_professional_matches.csv")
PROF_CSV       = _PROF_CSV_P2 if os.path.exists(_PROF_CSV_P2) else _PROF_CSV_P1

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "npi-review-dev-only")


def _safe_int(value, default, allowed=None):
    try:
        n = int(value)
    except (ValueError, TypeError):
        return default
    if allowed is not None and n not in allowed:
        return default
    return n


def _safe_back(url, fallback="/"):
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


_STATE_ABBR = {
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
}

def abbr_state(s):
    if not s:
        return ""
    s = s.strip()
    if len(s) <= 2:
        return s.upper()
    return _STATE_ABBR.get(s, s[:2].upper())


_CREDENTIAL_KEYWORDS = {
    "Physician":                           ["MD", "DO", "MBBS", "M.D", "D.O"],
    "Nurse":                               ["RN", "NP", "APRN", "FNP", "CNP", "CRNP", "APN"],
    "Social Worker":                       ["LCSW", "MSW", "LISW", "LMSW", "CSW"],
    "Rehabilitation Therapist (PT/OT/ET)": ["PT", "OT", "DPT", "MPT", "LPT"],
    "Transplant Coordinator":              ["RN", "NP", "APRN", "BSN"],
    "Case Manager":                        ["RN", "LCSW", "MSW"],
}



def matched_credential(hhl_type_label, nppes_credential):
    """Return the specific keyword that fired the credential signal, not the raw string."""
    keywords = _CREDENTIAL_KEYWORDS.get(hhl_type_label, [])
    cred_upper = (nppes_credential or "").upper()
    for kw in keywords:
        if kw in cred_upper:
            return kw
    return nppes_credential or ""


# ---------------------------------------------------------------------------
# Decisions database
# ---------------------------------------------------------------------------

def get_decisions_conn():
    conn = sqlite3.connect(DECISIONS_DB)
    conn.row_factory = sqlite3.Row

    # Migrate old schema (PK on hhl_id, hhl_type) to new (hhl_id, hhl_type, nppes_npi)
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
    )
    existing = cur.fetchone()
    if existing and "PRIMARY KEY (hhl_id, hhl_type)" in (existing[0] or ""):
        conn.execute("ALTER TABLE decisions RENAME TO _decisions_old")
        conn.execute("""
            CREATE TABLE decisions (
                hhl_id      TEXT,
                hhl_type    TEXT,
                nppes_npi   TEXT,
                decision    TEXT,
                notes       TEXT DEFAULT '',
                decided_at  TEXT,
                PRIMARY KEY (hhl_id, hhl_type, nppes_npi)
            )
        """)
        conn.execute("INSERT INTO decisions SELECT * FROM _decisions_old")
        conn.execute("DROP TABLE _decisions_old")
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                hhl_id      TEXT,
                hhl_type    TEXT,
                nppes_npi   TEXT,
                decision    TEXT,
                notes       TEXT DEFAULT '',
                decided_at  TEXT,
                PRIMARY KEY (hhl_id, hhl_type, nppes_npi)
            )
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS center_complete (
            hhl_id       TEXT PRIMARY KEY,
            completed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_multi_npi (
            hhl_id          TEXT PRIMARY KEY,
            created_at      TEXT,
            collapsed       INTEGER DEFAULT 0,
            override_single INTEGER DEFAULT 0
        )
    """)
    # Migrate: add override_single column if created before this change
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(manual_multi_npi)").fetchall()}
    if "override_single" not in existing_cols:
        conn.execute("ALTER TABLE manual_multi_npi ADD COLUMN override_single INTEGER DEFAULT 0")
    conn.commit()
    return conn


def get_all_decisions():
    """Returns dict keyed by (hhl_id, hhl_type, nppes_npi) -> decision row.
    Only 3-tuple keys — one entry per individual NPI decision.
    """
    conn = get_decisions_conn()
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = dict(r)
        key = (d["hhl_id"], d["hhl_type"], d["nppes_npi"] or "")
        result[key] = d
    return result


def _group_decisions_by_record(decisions):
    """Group 3-tuple decisions by (hhl_id, hhl_type) -> list of decision rows."""
    groups = {}
    for (hhl_id, hhl_type, _npi), d in decisions.items():
        groups.setdefault((hhl_id, hhl_type), []).append(d)
    return groups


def _effective_decision(record_rows, hhl_id, complete_ids, has_sibling):
    """Derive the effective (decision_row, status_string) for one record.

    status_string is one of: 'PENDING', 'APPROVED', 'REJECTED', 'FLAGGED'.

    Rules:
    - No rows -> PENDING.
    - Whole-record row (nppes_npi == '') -> its decision takes precedence (REJECTED/FLAGGED).
    - Sibling center: APPROVED only when hhl_id is in complete_ids; else PENDING.
    - Non-sibling: use the most-recently-written row's decision.
    """
    if not record_rows:
        return None, "PENDING"

    whole = [r for r in record_rows if not r.get("nppes_npi")]
    if whole:
        d = max(whole, key=lambda r: r["decided_at"])
        return d, d["decision"]

    if has_sibling:
        if hhl_id in complete_ids:
            d = max(record_rows, key=lambda r: r["decided_at"])
            return d, "APPROVED"
        return None, "PENDING"

    d = max(record_rows, key=lambda r: r["decided_at"])
    return d, d["decision"]


def get_center_approved_npis(hhl_id):
    """Return list of approved NPI strings for a center, ordered by decided_at asc."""
    conn = get_decisions_conn()
    rows = conn.execute(
        "SELECT nppes_npi FROM decisions WHERE hhl_id=? AND hhl_type='center' AND decision='APPROVED' ORDER BY decided_at",
        (hhl_id,)
    ).fetchall()
    conn.close()
    return [r["nppes_npi"] for r in rows if r["nppes_npi"]]


def get_complete_center_ids():
    """Return set of hhl_ids that have been marked complete."""
    conn = get_decisions_conn()
    rows = conn.execute("SELECT hhl_id FROM center_complete").fetchall()
    conn.close()
    return {r["hhl_id"] for r in rows}


def save_center_complete(hhl_id):
    conn = get_decisions_conn()
    conn.execute(
        "INSERT OR REPLACE INTO center_complete (hhl_id, completed_at) VALUES (?, ?)",
        (hhl_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def delete_center_complete(hhl_id):
    conn = get_decisions_conn()
    conn.execute("DELETE FROM center_complete WHERE hhl_id=?", (hhl_id,))
    conn.commit()
    conn.close()


def get_manual_multi_npi():
    """Return dict: hhl_id -> {'collapsed': bool, 'override_single': bool}"""
    conn = get_decisions_conn()
    rows = conn.execute("SELECT hhl_id, collapsed, override_single FROM manual_multi_npi").fetchall()
    conn.close()
    return {r["hhl_id"]: {"collapsed": bool(r["collapsed"]), "override_single": bool(r["override_single"])} for r in rows}


def delete_manual_multi_npi(hhl_id):
    conn = get_decisions_conn()
    conn.execute("DELETE FROM manual_multi_npi WHERE hhl_id=?", (hhl_id,))
    conn.commit()
    conn.close()


def get_approved_by_npi(hhl_type, groups):
    """Return dict: nppes_npi -> list of {hhl_id, name} for APPROVED decisions of this type."""
    decisions = get_all_decisions()
    result = {}
    for (hhl_id, htype, npi), d in decisions.items():
        if htype != hhl_type or d["decision"] != "APPROVED" or not npi:
            continue
        name = groups.get(hhl_id, {}).get("meta", {}).get("hhl_name", hhl_id)
        result.setdefault(npi, []).append({"hhl_id": hhl_id, "name": name})
    return result


def save_decision(hhl_id, hhl_type, nppes_npi, decision, notes=""):
    conn = get_decisions_conn()
    conn.execute("""
        INSERT OR REPLACE INTO decisions (hhl_id, hhl_type, nppes_npi, decision, notes, decided_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (hhl_id, hhl_type, nppes_npi, decision, notes,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Load and group match CSVs
# ---------------------------------------------------------------------------

def load_match_csv(path):
    """Return dict: hhl_id -> { meta: {...}, candidates: [...sorted by rank...] }"""
    if not os.path.exists(path):
        return {}

    groups = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hhl_id = row.get("hhl_id") or row.get("hhl_role_id", "")
            if hhl_id not in groups:
                groups[hhl_id] = {"meta": {}, "candidates": []}

            if not groups[hhl_id]["meta"]:
                # Store HHL-side fields once
                if "hhl_name" in row:
                    groups[hhl_id]["meta"] = {
                        "hhl_id":    row.get("hhl_id", ""),
                        "hhl_name":  row.get("hhl_name", ""),
                        "hhl_state": row.get("hhl_state", ""),
                        "hhl_city":  row.get("hhl_city", ""),
                        "hhl_url":   row.get("hhl_url", ""),
                        "type":      "center",
                    }
                else:
                    groups[hhl_id]["meta"] = {
                        "hhl_id":          row.get("hhl_role_id", ""),
                        "hhl_name":        f"{row.get('hhl_first_name','')} {row.get('hhl_last_name','')}".strip(),
                        "hhl_first_name":  row.get("hhl_first_name", ""),
                        "hhl_last_name":   row.get("hhl_last_name", ""),
                        "hhl_type":        row.get("hhl_type", ""),
                        "hhl_email":       row.get("hhl_email", ""),
                        "hhl_phone":       row.get("hhl_phone", ""),
                        "hhl_center_id":   row.get("hhl_medical_center_id", ""),
                        "hhl_center_name": row.get("hhl_medical_center_name", ""),
                        "hhl_state":       row.get("hhl_state", ""),
                        "type":            "professional",
                    }

            if row.get("nppes_npi"):
                groups[hhl_id]["candidates"].append(row)

    # Sort candidates by rank
    for g in groups.values():
        g["candidates"].sort(key=lambda r: int(r.get("rank") or 999))

    return groups


# Cache match data in memory (reload on demand)
_match_cache = {}

def get_centers():
    if "centers" not in _match_cache:
        _match_cache["centers"] = load_match_csv(CENTER_CSV)
    return _match_cache["centers"]

def get_professionals():
    if "professionals" not in _match_cache:
        _match_cache["professionals"] = load_match_csv(PROF_CSV)
    return _match_cache["professionals"]

def reload_cache():
    _match_cache.clear()


# ---------------------------------------------------------------------------
# NPPES database helpers
# ---------------------------------------------------------------------------

def get_nppes_conn():
    if not os.path.exists(NPPES_DB):
        return None
    conn = sqlite3.connect(NPPES_DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_provider_full(npi):
    conn = get_nppes_conn()
    if not conn:
        return None

    cur = conn.cursor()

    cur.execute("SELECT * FROM providers WHERE npi = ?", (npi,))
    provider = cur.fetchone()
    if not provider:
        conn.close()
        return None
    provider = dict(provider)

    cur.execute("SELECT * FROM taxonomies WHERE npi = ?", (npi,))
    provider["taxonomies"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM other_names WHERE npi = ?", (npi,))
    provider["other_names"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM practice_locations WHERE npi = ?", (npi,))
    provider["practice_locations"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM endpoints WHERE npi = ?", (npi,))
    provider["endpoints"] = [dict(r) for r in cur.fetchall()]

    conn.close()
    return provider


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

def build_stats():
    centers_data  = get_centers()
    profs_data    = get_professionals()
    decisions     = get_all_decisions()
    complete_ids  = get_complete_center_ids()
    by_record     = _group_decisions_by_record(decisions)
    manual_multi  = get_manual_multi_npi()

    def _eff(hhl_id, hhl_type, candidates):
        is_auto_sibling = hhl_type == "center" and any(
            "sibling_location" in (c.get("confidence_flags") or "")
            for c in candidates
        )
        m = manual_multi.get(hhl_id) if hhl_type == "center" else None
        is_sibling_overridden = bool(m) and bool(m.get("override_single", False))
        is_manual_multi_on = bool(m) and not is_sibling_overridden
        has_sibling = (is_auto_sibling and not is_sibling_overridden) or is_manual_multi_on
        rows = by_record.get((hhl_id, hhl_type), [])
        _, status = _effective_decision(rows, hhl_id, complete_ids, has_sibling)
        return status

    def count_confidence(groups):
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO_MATCH": 0, "SKIPPED": 0}
        for g in groups.values():
            c = g["candidates"][0].get("confidence", "NO_MATCH") if g["candidates"] else "NO_MATCH"
            counts[c] = counts.get(c, 0) + 1
        return counts

    def count_decisions(groups, hhl_type):
        counts = {"APPROVED": 0, "REJECTED": 0, "FLAGGED": 0, "PENDING": 0}
        for hhl_id, g in groups.items():
            status = _eff(hhl_id, hhl_type, g["candidates"])
            counts[status] = counts.get(status, 0) + 1
        return counts

    def count_pending_high(groups, hhl_type):
        count = 0
        for hhl_id, g in groups.items():
            status = _eff(hhl_id, hhl_type, g["candidates"])
            if status == "PENDING" and g["candidates"] and g["candidates"][0].get("confidence") == "HIGH":
                count += 1
        return count

    return {
        "center_confidence":   count_confidence(centers_data),
        "center_decisions":    count_decisions(centers_data, "center"),
        "center_total":        len(centers_data),
        "center_pending_high": count_pending_high(centers_data, "center"),
        "prof_confidence":     count_confidence(profs_data),
        "prof_decisions":      count_decisions(profs_data, "professional"),
        "prof_total":          len(profs_data),
        "prof_pending_high":   count_pending_high(profs_data, "professional"),
    }


def get_progress(groups, decisions, hhl_type, manual_multi=None):
    if manual_multi is None:
        manual_multi = {}
    complete_ids = get_complete_center_ids() if hhl_type == "center" else set()
    decisions_by_record = _group_decisions_by_record(decisions)
    total = approved = rejected = flagged = pending = 0
    for hhl_id, g in groups.items():
        total += 1
        is_auto_sibling = hhl_type == "center" and any(
            "sibling_location" in (c.get("confidence_flags") or "")
            for c in g["candidates"]
        )
        m = manual_multi.get(hhl_id) if hhl_type == "center" else None
        is_sibling_overridden = bool(m) and bool(m.get("override_single", False))
        is_manual_multi_on = bool(m) and not is_sibling_overridden
        has_sibling = (is_auto_sibling and not is_sibling_overridden) or is_manual_multi_on
        record_rows = decisions_by_record.get((hhl_id, hhl_type), [])
        _, eff_status = _effective_decision(record_rows, hhl_id, complete_ids, has_sibling)
        if eff_status == "APPROVED":
            approved += 1
        elif eff_status == "REJECTED":
            rejected += 1
        elif eff_status == "FLAGGED":
            flagged += 1
        else:
            pending += 1
    reviewed = approved + rejected + flagged
    pct      = round(reviewed / total * 100) if total else 0
    scale    = lambda n: round(n / total * 100, 1) if total else 0
    return {"total": total, "approved": approved, "rejected": rejected,
            "flagged": flagged, "pending": pending,
            "reviewed": reviewed, "pct": pct,
            "pct_approved": scale(approved),
            "pct_rejected": scale(rejected),
            "pct_flagged":  scale(flagged)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats = build_stats()
    return render_template("index.html", stats=stats)


CONF_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NO_MATCH": 3, "NO_STATE": 4, "SKIPPED": 5}


def _build_items(groups, decisions, hhl_type, filter_conf, filter_dec, search, filter_state, sort, manual_multi=None, filter_sibling=False):
    if manual_multi is None:
        manual_multi = {}
    complete_ids = get_complete_center_ids() if hhl_type == "center" else set()
    decisions_by_record = _group_decisions_by_record(decisions)

    # Pre-compute approved NPIs per record from the decisions dict
    approved_by_record = {}
    for (hid, htype, npi), d in decisions.items():
        if htype == hhl_type and d["decision"] == "APPROVED" and npi:
            approved_by_record.setdefault((hid, htype), []).append(npi)

    items = []
    for hhl_id, g in groups.items():
        is_auto_sibling = hhl_type == "center" and any(
            "sibling_location" in (c.get("confidence_flags") or "")
            for c in g["candidates"]
        )
        manual_info = manual_multi.get(hhl_id) if hhl_type == "center" else None
        is_sibling_overridden = bool(manual_info) and bool(manual_info.get("override_single", False))
        is_manual_multi = bool(manual_info) and not is_sibling_overridden
        manual_collapsed = manual_info.get("collapsed", False) if (manual_info and not is_sibling_overridden) else False
        has_sibling = (is_auto_sibling and not is_sibling_overridden) or is_manual_multi
        top_conf = g["candidates"][0].get("confidence", "NO_MATCH") if g["candidates"] else "NO_MATCH"
        record_rows = decisions_by_record.get((hhl_id, hhl_type), [])
        eff_decision, eff_status = _effective_decision(record_rows, hhl_id, complete_ids, has_sibling)

        if filter_conf != "all" and top_conf != filter_conf:
            continue
        if filter_dec != "all" and eff_status != filter_dec:
            continue
        if filter_state != "all" and g["meta"].get("hhl_state", "") != filter_state:
            continue
        if search:
            if search.lower() not in g["meta"].get("hhl_name", "").lower():
                continue
        if filter_sibling and not has_sibling:
            continue

        top_score = 0.0
        if g["candidates"]:
            try:
                top_score = float(g["candidates"][0].get("match_score", 0))
            except (ValueError, TypeError):
                pass

        approved_npis = approved_by_record.get((hhl_id, hhl_type), [])
        approved_candidates = [
            next((c for c in g["candidates"] if c.get("nppes_npi") == npi), {"nppes_npi": npi})
            for npi in approved_npis
        ]
        sibling_count = sum(
            1 for c in g["candidates"]
            if "sibling_location" in (c.get("confidence_flags") or "")
        )

        items.append({
            "hhl_id":             hhl_id,
            "meta":               g["meta"],
            "candidates":         g["candidates"],
            "decision":           eff_decision,
            "decision_status":    eff_status,
            "approved_npis":      approved_npis,
            "approved_npi_count": len(approved_npis),
            "approved_candidates": approved_candidates,
            "sibling_count":      sibling_count,
            "top_conf":           top_conf,
            "top_score":          top_score,
            "is_decided":         eff_status != "PENDING",
            "is_complete":        (hhl_id in complete_ids) if hhl_type == "center" else False,
            "has_sibling":          has_sibling if hhl_type == "center" else False,
            "is_auto_sibling":      is_auto_sibling if hhl_type == "center" else False,
            "is_sibling_overridden": is_sibling_overridden,
            "is_manual_multi":      is_manual_multi,
            "manual_collapsed":     manual_collapsed,
        })

    if sort == "score":
        items.sort(key=lambda x: x["top_score"], reverse=True)
    elif sort == "name":
        items.sort(key=lambda x: x["meta"].get("hhl_name", "").lower())
    elif sort == "state":
        items.sort(key=lambda x: x["meta"].get("hhl_state", ""))
    else:
        items.sort(key=lambda x: (CONF_ORDER.get(x["top_conf"], 9), -x["top_score"]))

    return items


def _all_states(groups):
    states = sorted({g["meta"].get("hhl_state", "") for g in groups.values() if g["meta"].get("hhl_state")})
    return states


@app.route("/centers")
def centers():
    groups      = get_centers()
    decisions   = get_all_decisions()
    filter_conf  = request.args.get("conf", "all")
    filter_dec   = request.args.get("dec", "all")
    filter_state = request.args.get("state", "all")
    search       = request.args.get("q", "").strip()
    sort         = request.args.get("sort", "conf")
    per_page     = _safe_int(request.args.get("per_page"), 50, {50, 100, 250, 999999})
    page         = max(1, _safe_int(request.args.get("page"), 1))

    filter_sibling  = request.args.get("sibling", "0") == "1"
    manual_multi    = get_manual_multi_npi()
    items           = _build_items(groups, decisions, "center", filter_conf, filter_dec, search, filter_state, sort, manual_multi, filter_sibling)
    states          = _all_states(groups)
    approved_by_npi = get_approved_by_npi("center", groups)
    progress        = get_progress(groups, decisions, "center", manual_multi)
    total           = len(items)
    start           = (page - 1) * per_page
    paged           = items[start:start + per_page]

    return render_template("centers.html",
        items=paged, total=total, page=page, per_page=per_page,
        filter_conf=filter_conf, filter_dec=filter_dec,
        filter_state=filter_state, search=search, sort=sort, states=states,
        approved_by_npi=approved_by_npi, progress=progress,
        decisions=decisions, filter_sibling=filter_sibling)


@app.route("/professionals")
def professionals():
    groups      = get_professionals()
    decisions   = get_all_decisions()
    filter_conf  = request.args.get("conf", "all")
    filter_dec   = request.args.get("dec", "all")
    filter_state = request.args.get("state", "all")
    search       = request.args.get("q", "").strip()
    sort         = request.args.get("sort", "conf")
    per_page     = _safe_int(request.args.get("per_page"), 50, {50, 100, 250, 999999})
    page         = max(1, _safe_int(request.args.get("page"), 1))

    items           = _build_items(groups, decisions, "professional", filter_conf, filter_dec, search, filter_state, sort)
    states          = _all_states(groups)
    approved_by_npi = get_approved_by_npi("professional", groups)
    progress        = get_progress(groups, decisions, "professional")
    total           = len(items)
    start           = (page - 1) * per_page
    paged           = items[start:start + per_page]

    return render_template("professionals.html",
        items=paged, total=total, page=page, per_page=per_page,
        filter_conf=filter_conf, filter_dec=filter_dec,
        filter_state=filter_state, search=search, sort=sort, states=states,
        approved_by_npi=approved_by_npi, progress=progress,
        decisions=decisions)


@app.route("/approved")
def approved_redirect():
    return redirect(url_for("decisions_page"))


@app.route("/decisions")
def decisions_page():
    all_decisions = get_all_decisions()
    centers       = get_centers()
    professionals = get_professionals()

    decisions_by_record = _group_decisions_by_record(all_decisions)
    complete_ids_all = get_complete_center_ids()
    manual_multi_all = get_manual_multi_npi()

    tab          = request.args.get("tab", "all")
    search       = request.args.get("q", "").strip().lower()
    filter_state = request.args.get("state", "all")
    filter_type  = request.args.get("hhl_type", "all")

    _sort_cols = ["name", "type", "state", "decision", "date"]
    sorts = {}
    for col in _sort_cols:
        val = request.args.get(f"sort_{col}", "")
        if val in ("asc", "desc"):
            sorts[col] = val

    dec_filter = {"approved": "APPROVED", "rejected": "REJECTED", "flagged": "FLAGGED"}.get(tab)

    rows = []
    for (hhl_id, hhl_type), record_rows in decisions_by_record.items():
        grps   = centers if hhl_type == "center" else professionals
        g      = grps.get(hhl_id, {})
        meta   = g.get("meta", {})
        cands  = g.get("candidates", [])
        is_auto_sibling = hhl_type == "center" and any(
            "sibling_location" in (c.get("confidence_flags") or "")
            for c in cands
        )
        _m = manual_multi_all.get(hhl_id) if hhl_type == "center" else None
        _overridden = bool(_m) and bool(_m.get("override_single", False))
        has_sibling = (is_auto_sibling and not _overridden) or (bool(_m) and not _overridden)
        eff_decision, eff_status = _effective_decision(record_rows, hhl_id, complete_ids_all, has_sibling)

        if eff_status == "PENDING":
            continue
        if dec_filter and eff_status != dec_filter:
            continue

        # For display: use effective decision's NPI if available, else rank-1 candidate.
        display_npi = eff_decision.get("nppes_npi") if eff_decision else None
        if not display_npi and cands:
            display_npi = cands[0].get("nppes_npi")
        chosen = next((c for c in cands if c.get("nppes_npi") == display_npi), cands[0] if cands else {})

        name  = meta.get("hhl_name", hhl_id)
        state = abbr_state(meta.get("hhl_state", ""))

        if search and search not in name.lower():
            continue
        if filter_state != "all" and state != filter_state:
            continue
        if filter_type != "all" and hhl_type != filter_type:
            continue

        signals_raw = (chosen.get("signals_matched") or "")
        signals = [s.strip() for s in signals_raw.split("|") if s.strip()]

        item = {
            "hhl_id":              hhl_id,
            "hhl_type":            hhl_type,
            "name":                name,
            "hhl_state":           state,
            "hhl_type_label":      meta.get("hhl_type", ""),
            "decision":            eff_status,
            "nppes_npi":           display_npi or "",
            "nppes_candidate_npi": chosen.get("nppes_npi", ""),
            "nppes_name":          (chosen.get("nppes_name") or
                                    f"{chosen.get('nppes_first_name','')} {chosen.get('nppes_last_name','')}".strip()),
            "confidence":          chosen.get("confidence", ""),
            "match_score":         chosen.get("match_score", ""),
            "signals":             signals,
            "nppes_credential":    chosen.get("nppes_credential", ""),
            "nppes_taxonomy_code": chosen.get("nppes_taxonomy_code", ""),
            "nppes_phone":         chosen.get("nppes_phone", ""),
            "nppes_city":          chosen.get("nppes_city", ""),
            "nppes_state":         chosen.get("nppes_state", ""),
            "notes":               (eff_decision.get("notes", "") if eff_decision else ""),
            "decided_at":          (eff_decision.get("decided_at") or "")[:10] if eff_decision else "",
            "decided_at_full":     (eff_decision.get("decided_at") or "") if eff_decision else "",
        }
        if hhl_type == "center":
            item["approved_npis"] = get_center_approved_npis(hhl_id)
        else:
            item["approved_npis"] = [r["nppes_npi"] for r in record_rows if r.get("decision") == "APPROVED" and r.get("nppes_npi")]
        rows.append(item)

    # Fixed priority hierarchy: type > decision > state > date > name.
    # Apply as a stable-sort cascade in reverse priority (least important first).
    _DECISION_ORDER = {"FLAGGED": 0, "APPROVED": 1, "REJECTED": 2}
    _TYPE_ORDER     = {"center": 0, "professional": 1}
    # When both name and date are active, group by calendar day (not by exact time).
    date_key = "decided_at" if ("name" in sorts and "date" in sorts) else "decided_at_full"
    _sort_key_map = {
        "name":     lambda r: r["name"].lower(),
        "type":     lambda r: _TYPE_ORDER.get(r["hhl_type"], 99),
        "state":    lambda r: r["hhl_state"],
        "decision": lambda r: _DECISION_ORDER.get(r["decision"], 99),
        "date":     lambda r: r[date_key],
    }
    _priority = ["name", "date", "state", "decision", "type"]
    if sorts:
        for col in _priority:
            if col not in sorts:
                continue
            # Date: ↑ (asc) = newest first, ↓ (desc) = oldest first — inverted from alphabetical convention.
            if col == "date":
                reverse = (sorts[col] == "asc")
            else:
                reverse = (sorts[col] == "desc")
            rows.sort(key=_sort_key_map[col], reverse=reverse)
    else:
        rows.sort(key=lambda r: (r["hhl_type"], r["name"].lower()))

    # Count records by effective decision status
    eff_counts = {"APPROVED": 0, "REJECTED": 0, "FLAGGED": 0}
    for (hhl_id, hhl_type), record_rows in decisions_by_record.items():
        grps = centers if hhl_type == "center" else professionals
        g = grps.get(hhl_id, {})
        is_auto_sibling_2 = hhl_type == "center" and any(
            "sibling_location" in (c.get("confidence_flags") or "")
            for c in g.get("candidates", [])
        )
        _m2 = manual_multi_all.get(hhl_id) if hhl_type == "center" else None
        _overridden2 = bool(_m2) and bool(_m2.get("override_single", False))
        has_sibling = (is_auto_sibling_2 and not _overridden2) or (bool(_m2) and not _overridden2)
        _, s = _effective_decision(record_rows, hhl_id, complete_ids_all, has_sibling)
        if s in eff_counts:
            eff_counts[s] += 1
    counts = {
        "approved": eff_counts["APPROVED"],
        "rejected": eff_counts["REJECTED"],
        "flagged":  eff_counts["FLAGGED"],
        "all":      sum(eff_counts.values()),
    }

    state_set = set()
    for (hhl_id, hhl_type) in decisions_by_record:
        grps = centers if hhl_type == "center" else professionals
        s = abbr_state((grps.get(hhl_id, {}).get("meta") or {}).get("hhl_state", ""))
        if s:
            state_set.add(s)
    states = sorted(state_set)

    per_page = _safe_int(request.args.get("per_page"), 50, {25, 50, 100, 250, 999999})
    page     = max(1, _safe_int(request.args.get("page"), 1))
    total    = len(rows)
    paged    = rows[(page - 1) * per_page : page * per_page]

    complete_ids = get_complete_center_ids()

    return render_template("decisions.html",
        rows=paged, total=total, page=page, per_page=per_page,
        tab=tab, search=request.args.get("q", ""), counts=counts,
        filter_state=filter_state, filter_type=filter_type,
        states=states, sorts=sorts, complete_ids=complete_ids,
    )


@app.route("/undecide/<hhl_type>/<hhl_id>", methods=["POST"])
def undecide(hhl_type, hhl_id):
    back = _safe_back(request.form.get("back"), "/decisions")
    name = request.form.get("name", "").strip()
    conn = get_decisions_conn()
    conn.execute("DELETE FROM decisions WHERE hhl_id = ? AND hhl_type = ?", (hhl_id, hhl_type))
    conn.commit()
    conn.close()
    if hhl_type == "center":
        delete_center_complete(hhl_id)
        delete_manual_multi_npi(hhl_id)
    review_url = f"/{hhl_type}s?q={escape(name)}" if name else f"/{hhl_type}s"
    label = escape(name) if name else "Record"
    flash(Markup(
        f'Decision cleared for <strong>{label}</strong>. '
        f'<a href="{review_url}" style="color:inherit;font-weight:700;text-decoration:underline;">Re-review →</a>'
    ), "info")
    return redirect(back)


@app.route("/undecide-npi/<hhl_type>/<hhl_id>", methods=["POST"])
def undecide_npi(hhl_type, hhl_id):
    nppes_npi = request.form.get("nppes_npi", "").strip()
    back = _safe_back(request.form.get("back"), f"/{hhl_type}s")
    if not nppes_npi:
        return redirect(back)
    conn = get_decisions_conn()
    conn.execute(
        "DELETE FROM decisions WHERE hhl_id=? AND hhl_type=? AND nppes_npi=?",
        (hhl_id, hhl_type, nppes_npi)
    )
    conn.commit()
    conn.close()
    if not get_center_approved_npis(hhl_id):
        delete_center_complete(hhl_id)
        if hhl_type == "center":
            conn2 = get_decisions_conn()
            conn2.execute("UPDATE manual_multi_npi SET collapsed = 0 WHERE hhl_id = ?", (hhl_id,))
            conn2.commit()
            conn2.close()
    flash("NPI approval removed.", "info")
    return redirect(back)


@app.route("/complete-center/<hhl_id>", methods=["POST"])
def complete_center(hhl_id):
    back = _safe_back(request.form.get("back"), "/centers")
    groups = get_centers()
    g = groups.get(hhl_id)
    name = g["meta"]["hhl_name"] if g else hhl_id
    approved = get_center_approved_npis(hhl_id)
    if not approved:
        flash("Approve at least one NPI before marking complete.", "warn")
        return redirect(back)
    save_center_complete(hhl_id)
    flash(
        Markup(f'<strong>{escape(name)}</strong> marked complete — {len(approved)} NPI{"s" if len(approved) != 1 else ""} approved.'),
        "approved"
    )
    return redirect(back)


@app.route("/toggle-multi-npi/<hhl_id>", methods=["POST"])
def toggle_multi_npi(hhl_id):
    action = request.form.get("action", "")
    back = _safe_back(request.form.get("back"), "/centers")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_decisions_conn()
    if action == "enable":
        # Manually enable multi-NPI (non-sibling center, or re-enable after collapse)
        conn.execute(
            "INSERT OR REPLACE INTO manual_multi_npi (hhl_id, created_at, collapsed, override_single) VALUES (?, ?, 0, 0)",
            (hhl_id, now)
        )
        conn.commit()
        flash("Multiple NPIs enabled — approve each valid location, then click Mark Complete.", "info")
    elif action == "disable":
        # Manually disable (non-sibling center)
        approved = get_center_approved_npis(hhl_id)
        if approved:
            conn.execute("UPDATE manual_multi_npi SET collapsed = 1 WHERE hhl_id = ?", (hhl_id,))
        else:
            conn.execute("DELETE FROM manual_multi_npi WHERE hhl_id = ?", (hhl_id,))
        conn.commit()
        flash("Switched to single NPI.", "info")
    elif action == "override_disable":
        # Reviewer says auto-detected sibling is wrong — revert to single-NPI mode
        conn.execute(
            "INSERT OR REPLACE INTO manual_multi_npi (hhl_id, created_at, collapsed, override_single) VALUES (?, ?, 0, 1)",
            (hhl_id, now)
        )
        conn.commit()
        flash("Treated as single location — multiple NPI mode off.", "info")
    elif action == "override_enable":
        # Reviewer re-checks an overridden auto-sibling — restore sibling behavior
        conn.execute("DELETE FROM manual_multi_npi WHERE hhl_id = ?", (hhl_id,))
        conn.commit()
        flash("Multiple locations restored.", "info")
    conn.close()
    return redirect(back)


@app.route("/decide/<hhl_type>/<hhl_id>", methods=["POST"])
def decide(hhl_type, hhl_id):
    decision  = request.form.get("decision")
    nppes_npi = request.form.get("nppes_npi", "")
    notes     = request.form.get("notes", "")
    back      = _safe_back(request.form.get("back"), f"/{hhl_type}s")

    if decision in ("APPROVED", "REJECTED", "FLAGGED"):
        save_decision(hhl_id, hhl_type, nppes_npi, decision, notes)
        labels = {"APPROVED": "Match confirmed.", "REJECTED": "Marked no match.", "FLAGGED": "Flagged for review."}
        flash(labels[decision], decision.lower())

    return redirect(back)


@app.route("/bulk-approve/<hhl_type>", methods=["POST"])
def bulk_approve(hhl_type):
    if hhl_type not in ("center", "professional"):
        return redirect(url_for("index"))
    groups    = get_centers() if hhl_type == "center" else get_professionals()
    decisions = get_all_decisions()
    decisions_by_record = _group_decisions_by_record(decisions)
    approved  = 0

    for hhl_id, g in groups.items():
        if (hhl_id, hhl_type) in decisions_by_record:
            continue
        if g["candidates"] and g["candidates"][0].get("confidence") == "HIGH":
            top = g["candidates"][0]
            npi = top.get("nppes_npi", "")
            save_decision(hhl_id, hhl_type, npi, "APPROVED", "auto bulk-approved HIGH confidence")
            approved += 1

    label = "center" if hhl_type == "center" else "professional"
    flash(f"Bulk approved {approved} HIGH confidence {label}{'s' if approved != 1 else ''}.", "approved")
    return redirect(f"/{hhl_type}s?dec=APPROVED")


@app.route("/api/provider/<npi>")
def api_provider(npi):
    data = get_provider_full(npi)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)

@app.route("/provider/<npi>")
def provider(npi):
    nppes    = get_provider_full(npi)
    hhl_data = None
    sibling_npis = []  # list of {npi, name, city, state} for branch selector

    hhl_id_param = request.args.get("hhl_id", "").strip()

    if hhl_id_param:
        centers = get_centers()
        g = centers.get(hhl_id_param)
        if g:
            hhl_data = {"type": "center", **g["meta"]}
            approved = get_center_approved_npis(hhl_id_param)
            for approved_npi in approved:
                cand = next(
                    (c for c in g["candidates"] if c.get("nppes_npi") == approved_npi),
                    {}
                )
                sibling_npis.append({
                    "npi":   approved_npi,
                    "name":  cand.get("nppes_name", approved_npi),
                    "city":  cand.get("nppes_city", ""),
                    "state": cand.get("nppes_state", ""),
                })
    else:
        decisions = get_all_decisions()
        for (hhl_id, hhl_type, nppes_npi), d in decisions.items():
            if nppes_npi == npi and d.get("decision") == "APPROVED":
                if hhl_type == "center":
                    groups = get_centers()
                    g = groups.get(hhl_id)
                    if g:
                        hhl_data = {"type": "center", **g["meta"]}
                else:
                    groups = get_professionals()
                    g = groups.get(hhl_id)
                    if g:
                        hhl_data = {"type": "professional", **g["meta"]}
                break

    return render_template(
        "provider.html",
        nppes=nppes, hhl=hhl_data, npi=npi,
        sibling_npis=sibling_npis,
        hhl_id=hhl_id_param,
    )


@app.route("/export")
def export():
    centers = get_centers()
    profs   = get_professionals()

    rows = []

    # Centers: one row per approved NPI
    for hhl_id, g in centers.items():
        approved_npis = get_center_approved_npis(hhl_id)
        if not approved_npis:
            continue
        meta = g.get("meta", {})
        for npi in approved_npis:
            cand = next((c for c in g["candidates"] if c.get("nppes_npi") == npi), {})
            rows.append({
                "hhl_id":        meta.get("hhl_id", ""),
                "hhl_name":      meta.get("hhl_name", ""),
                "hhl_state":     meta.get("hhl_state", ""),
                "hhl_type":      "center",
                "nppes_npi":     npi,
                "nppes_name":    cand.get("nppes_name", ""),
                "nppes_address": cand.get("nppes_address", ""),
                "nppes_city":    cand.get("nppes_city", ""),
                "nppes_state":   cand.get("nppes_state", ""),
                "nppes_zip":     cand.get("nppes_zip", ""),
                "match_score":   cand.get("match_score", ""),
                "confidence":    cand.get("confidence", ""),
            })

    # Professionals: one row per approved decision
    decisions = get_all_decisions()
    for (hhl_id, htype, npi), d in decisions.items():
        if htype != "professional" or d["decision"] != "APPROVED" or not npi:
            continue
        g = profs.get(hhl_id, {})
        if not g:
            continue
        meta = g.get("meta", {})
        cand = next((c for c in g["candidates"] if c.get("nppes_npi") == npi), {})
        rows.append({
            "hhl_id":        meta.get("hhl_id", ""),
            "hhl_name":      meta.get("hhl_name", ""),
            "hhl_state":     meta.get("hhl_state", ""),
            "hhl_type":      "professional",
            "nppes_npi":     npi,
            "nppes_name":    f"{cand.get('nppes_first_name','')} {cand.get('nppes_last_name','')}".strip(),
            "nppes_address": cand.get("nppes_address", ""),
            "nppes_city":    cand.get("nppes_city", ""),
            "nppes_state":   cand.get("nppes_state", ""),
            "nppes_zip":     cand.get("nppes_zip", ""),
            "match_score":   cand.get("match_score", ""),
            "confidence":    cand.get("confidence", ""),
        })

    if not rows:
        flash("No approved decisions to export.", "info")
        return redirect(url_for("decisions_page"))

    output = io.StringIO()
    fieldnames = ["hhl_id", "hhl_name", "hhl_state", "hhl_type",
                  "nppes_npi", "nppes_name", "nppes_address", "nppes_city",
                  "nppes_state", "nppes_zip", "match_score", "confidence"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=hhl_confirmed.csv"},
    )


@app.route("/reload")
def reload_data():
    reload_cache()
    return redirect(url_for("index"))


if __name__ == "__main__":
    print("Starting NPI Review Site at http://localhost:5001")
    app.run(debug=True, port=5001)
