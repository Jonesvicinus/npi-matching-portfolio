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
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for, Response

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


# ---------------------------------------------------------------------------
# Decisions database
# ---------------------------------------------------------------------------

def get_decisions_conn():
    conn = sqlite3.connect(DECISIONS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            hhl_id      TEXT,
            hhl_type    TEXT,
            nppes_npi   TEXT,
            decision    TEXT,
            notes       TEXT DEFAULT '',
            decided_at  TEXT,
            PRIMARY KEY (hhl_id, hhl_type)
        )
    """)
    conn.commit()
    return conn


def get_all_decisions():
    conn = get_decisions_conn()
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    conn.close()
    return {(r["hhl_id"], r["hhl_type"]): dict(r) for r in rows}


def get_approved_by_npi(hhl_type, groups):
    """Return dict: nppes_npi -> list of {hhl_id, name} for APPROVED decisions of this type."""
    decisions = get_all_decisions()
    result = {}
    for (hhl_id, dtype), d in decisions.items():
        if dtype != hhl_type or d["decision"] != "APPROVED" or not d.get("nppes_npi"):
            continue
        npi = d["nppes_npi"]
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
    centers       = get_centers()
    professionals = get_professionals()
    decisions     = get_all_decisions()

    def count_confidence(groups, hhl_type):
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NO_MATCH": 0, "SKIPPED": 0}
        for g in groups.values():
            if g["candidates"]:
                c = g["candidates"][0].get("confidence", "LOW")
                counts[c] = counts.get(c, 0) + 1
            else:
                conf = g["meta"].get("confidence", "NO_MATCH")
                counts[conf] = counts.get(conf, 0) + 1
        return counts

    def count_decisions(groups, hhl_type):
        counts = {"APPROVED": 0, "REJECTED": 0, "FLAGGED": 0, "PENDING": 0}
        for hhl_id in groups:
            d = decisions.get((hhl_id, hhl_type))
            if d:
                counts[d["decision"]] = counts.get(d["decision"], 0) + 1
            else:
                counts["PENDING"] += 1
        return counts

    return {
        "center_confidence":      count_confidence(centers, "center"),
        "center_decisions":       count_decisions(centers, "center"),
        "center_total":           len(centers),
        "prof_confidence":        count_confidence(professionals, "professional"),
        "prof_decisions":         count_decisions(professionals, "professional"),
        "prof_total":             len(professionals),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats = build_stats()
    return render_template("index.html", stats=stats)


CONF_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NO_MATCH": 3, "NO_STATE": 4, "SKIPPED": 5}


def _build_items(groups, decisions, hhl_type, filter_conf, filter_dec, search, filter_state, sort):
    items = []
    for hhl_id, g in groups.items():
        d        = decisions.get((hhl_id, hhl_type))
        top_conf = g["candidates"][0].get("confidence", "NO_MATCH") if g["candidates"] else "NO_MATCH"
        dec_val  = d["decision"] if d else "PENDING"

        if filter_conf != "all" and top_conf != filter_conf:
            continue
        if filter_dec != "all" and dec_val != filter_dec:
            continue
        if filter_state != "all" and g["meta"].get("hhl_state", "") != filter_state:
            continue
        if search:
            name = g["meta"].get("hhl_name", "").lower()
            if search.lower() not in name:
                continue

        top_score = 0.0
        if g["candidates"]:
            try:
                top_score = float(g["candidates"][0].get("match_score", 0))
            except (ValueError, TypeError):
                pass

        items.append({
            "hhl_id":    hhl_id,
            "meta":      g["meta"],
            "candidates": g["candidates"],
            "decision":  d,
            "top_conf":  top_conf,
            "top_score": top_score,
        })

    if sort == "score":
        items.sort(key=lambda x: x["top_score"], reverse=True)
    elif sort == "name":
        items.sort(key=lambda x: x["meta"].get("hhl_name", "").lower())
    elif sort == "state":
        items.sort(key=lambda x: x["meta"].get("hhl_state", ""))
    else:  # default: confidence then score
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
    per_page     = int(request.args.get("per_page", 50))
    page         = int(request.args.get("page", 1))
    if per_page not in (50, 100, 250, 999999):
        per_page = 50

    items           = _build_items(groups, decisions, "center", filter_conf, filter_dec, search, filter_state, sort)
    states          = _all_states(groups)
    approved_by_npi = get_approved_by_npi("center", groups)
    total           = len(items)
    start           = (page - 1) * per_page
    paged           = items[start:start + per_page]

    return render_template("centers.html",
        items=paged, total=total, page=page, per_page=per_page,
        filter_conf=filter_conf, filter_dec=filter_dec,
        filter_state=filter_state, search=search, sort=sort, states=states,
        approved_by_npi=approved_by_npi)


@app.route("/professionals")
def professionals():
    groups      = get_professionals()
    decisions   = get_all_decisions()
    filter_conf  = request.args.get("conf", "all")
    filter_dec   = request.args.get("dec", "all")
    filter_state = request.args.get("state", "all")
    search       = request.args.get("q", "").strip()
    sort         = request.args.get("sort", "conf")
    per_page     = int(request.args.get("per_page", 50))
    page         = int(request.args.get("page", 1))
    if per_page not in (50, 100, 250, 999999):
        per_page = 50

    items           = _build_items(groups, decisions, "professional", filter_conf, filter_dec, search, filter_state, sort)
    states          = _all_states(groups)
    approved_by_npi = get_approved_by_npi("professional", groups)
    total           = len(items)
    start           = (page - 1) * per_page
    paged           = items[start:start + per_page]

    return render_template("professionals.html",
        items=paged, total=total, page=page, per_page=per_page,
        filter_conf=filter_conf, filter_dec=filter_dec,
        filter_state=filter_state, search=search, sort=sort, states=states,
        approved_by_npi=approved_by_npi)


@app.route("/approved")
def approved_redirect():
    return redirect(url_for("decisions_page"))


@app.route("/decisions")
def decisions_page():
    all_decisions = get_all_decisions()
    centers       = get_centers()
    professionals = get_professionals()
    tab           = request.args.get("tab", "approved")
    search        = request.args.get("q", "").strip().lower()

    # tab controls which decision type(s) to show
    dec_filter = {"approved": "APPROVED", "rejected": "REJECTED", "flagged": "FLAGGED"}.get(tab)

    rows = []
    for (hhl_id, hhl_type), d in all_decisions.items():
        if dec_filter and d["decision"] != dec_filter:
            continue

        groups = centers if hhl_type == "center" else professionals
        g      = groups.get(hhl_id, {})
        meta   = g.get("meta", {})
        cands  = g.get("candidates", [])
        chosen = next((c for c in cands if c.get("nppes_npi") == d.get("nppes_npi")), cands[0] if cands else {})

        name = meta.get("hhl_name", hhl_id)
        if search and search not in name.lower():
            continue

        rows.append({
            "hhl_id":         hhl_id,
            "hhl_type":       hhl_type,
            "name":           name,
            "hhl_state":      meta.get("hhl_state", ""),
            "hhl_type_label": meta.get("hhl_type", ""),
            "decision":       d["decision"],
            "nppes_npi":      d.get("nppes_npi", ""),
            "nppes_name":     chosen.get("nppes_name") or f"{chosen.get('nppes_first_name','')} {chosen.get('nppes_last_name','')}".strip(),
            "confidence":     chosen.get("confidence", ""),
            "notes":          d.get("notes", ""),
            "decided_at":     (d.get("decided_at") or "")[:10],
        })

    rows.sort(key=lambda r: (r["hhl_type"], r["name"].lower()))

    counts = {}
    for label, dec in [("approved", "APPROVED"), ("rejected", "REJECTED"), ("flagged", "FLAGGED")]:
        counts[label] = sum(1 for d in all_decisions.values() if d["decision"] == dec)
    counts["all"] = sum(counts.values())

    per_page = int(request.args.get("per_page", 50))
    if per_page not in (25, 50, 100, 250, 999999):
        per_page = 50
    page  = int(request.args.get("page", 1))
    total = len(rows)
    start = (page - 1) * per_page
    paged = rows[start:start + per_page]

    return render_template("decisions.html", rows=paged, total=total, page=page, per_page=per_page,
                           tab=tab, search=search, counts=counts)


@app.route("/undecide/<hhl_type>/<hhl_id>", methods=["POST"])
def undecide(hhl_type, hhl_id):
    back = request.form.get("back", "/decisions")
    conn = get_decisions_conn()
    conn.execute("DELETE FROM decisions WHERE hhl_id = ? AND hhl_type = ?", (hhl_id, hhl_type))
    conn.commit()
    conn.close()
    return redirect(back)


@app.route("/decide/<hhl_type>/<hhl_id>", methods=["POST"])
def decide(hhl_type, hhl_id):
    decision  = request.form.get("decision")
    nppes_npi = request.form.get("nppes_npi", "")
    notes     = request.form.get("notes", "")
    back      = request.form.get("back", f"/{hhl_type}s")

    if decision in ("APPROVED", "REJECTED", "FLAGGED"):
        save_decision(hhl_id, hhl_type, nppes_npi, decision, notes)

    return redirect(back)


@app.route("/bulk-approve/<hhl_type>", methods=["POST"])
def bulk_approve(hhl_type):
    groups    = get_centers() if hhl_type == "center" else get_professionals()
    decisions = get_all_decisions()
    approved  = 0

    for hhl_id, g in groups.items():
        if (hhl_id, hhl_type) in decisions:
            continue
        if g["candidates"] and g["candidates"][0].get("confidence") == "HIGH":
            top = g["candidates"][0]
            npi = top.get("nppes_npi", "")
            save_decision(hhl_id, hhl_type, npi, "APPROVED", "auto bulk-approved HIGH confidence")
            approved += 1

    return redirect(f"/{hhl_type}s?dec=APPROVED")


@app.route("/provider/<npi>")
def provider(npi):
    nppes = get_provider_full(npi)

    # Find HHL data for this NPI from decisions
    decisions = get_all_decisions()
    hhl_data  = None

    for (hhl_id, hhl_type), d in decisions.items():
        if d.get("nppes_npi") == npi and d.get("decision") == "APPROVED":
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

    return render_template("provider.html", nppes=nppes, hhl=hhl_data, npi=npi)


@app.route("/export")
def export():
    decisions = get_all_decisions()
    centers   = get_centers()
    profs     = get_professionals()

    rows = []
    for (hhl_id, hhl_type), d in decisions.items():
        if d["decision"] != "APPROVED":
            continue

        if hhl_type == "center":
            g = centers.get(hhl_id, {})
        else:
            g = profs.get(hhl_id, {})

        if not g:
            continue

        meta       = g.get("meta", {})
        candidates = g.get("candidates", [])
        chosen     = next((c for c in candidates if c.get("nppes_npi") == d["nppes_npi"]), {})

        rows.append({
            "hhl_type":      hhl_type,
            "hhl_id":        hhl_id,
            "hhl_name":      meta.get("hhl_name", ""),
            "hhl_email":     meta.get("hhl_email", ""),
            "hhl_center":    meta.get("hhl_center_name", "") or meta.get("hhl_name", ""),
            "hhl_state":     meta.get("hhl_state", ""),
            "hhl_prof_type": meta.get("hhl_type", ""),
            "nppes_npi":     d["nppes_npi"],
            "nppes_name":    chosen.get("nppes_name") or f"{chosen.get('nppes_first_name','')} {chosen.get('nppes_last_name','')}".strip(),
            "nppes_address": chosen.get("nppes_address", ""),
            "nppes_city":    chosen.get("nppes_city", ""),
            "nppes_state":   chosen.get("nppes_state", ""),
            "nppes_zip":     chosen.get("nppes_zip", ""),
            "nppes_phone":   chosen.get("nppes_phone", ""),
            "confidence":    chosen.get("confidence", ""),
            "match_score":   chosen.get("match_score", ""),
            "notes":         d.get("notes", ""),
            "decided_at":    d.get("decided_at", ""),
        })

    fields = [
        "hhl_type", "hhl_id", "hhl_name", "hhl_email", "hhl_center",
        "hhl_state", "hhl_prof_type", "nppes_npi", "nppes_name",
        "nppes_address", "nppes_city", "nppes_state", "nppes_zip", "nppes_phone",
        "confidence", "match_score", "notes", "decided_at",
    ]

    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)

    # Also save to file
    with open(CONFIRMED_CSV, "w", newline="", encoding="utf-8") as f:
        writer2 = csv.DictWriter(f, fieldnames=fields)
        writer2.writeheader()
        writer2.writerows(rows)

    return Response(
        buf.getvalue(),
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
