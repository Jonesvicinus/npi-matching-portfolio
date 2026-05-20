# NPI Matching Pipeline

Offline pipeline for matching healthcare provider records against the NPPES federal registry, with a web-based review interface for human verification.

Built for [Help Hope Live](https://helphopelive.org/), a nonprofit medical fundraising organization, to manage ~12,000 provider records with no NPI numbers on file. Manually looking up each provider was infeasible; this tool automates candidate retrieval and surfaces high-confidence matches for human approval.

## How it works

**Pipeline** (`pipeline/`)

1. `build_local_db.py` — imports the NPPES bulk CSV (~9.5M providers) into a local SQLite database with indexes optimized for name and location lookups.
2. `match_medical_centers.py` — matches facility records against NPPES org providers using fuzzy name matching, city/ZIP signals, and a uniqueness heuristic.
3. `match_medical_professionals.py` — phase 1 individual matching using fuzzy name, city, credential, and taxonomy signals.
4. `match_professionals_phase2.py` — phase 2 individual matching that anchors searches to a confirmed center's location, dramatically improving precision.

**Review site** (`review_site/`)

Flask web app for human review of match candidates. Reviewers see the best NPPES candidate for each provider record along with confidence signals, and can confirm, reject, or flag for further review. Approved center decisions feed back into phase 2 to improve subsequent professional matching runs.

## Confidence scoring

Matches are scored using multiple independent signals:

| Signal | Description |
|--------|-------------|
| `name` | Fuzzy name score ≥ 0.85 (token sort ratio) |
| `city` | Practice city matches provider city |
| `zip` | ZIP code matches |
| `credential` | NPPES credential aligns with provider type (e.g. MD, RN) |
| `taxonomy` | NPPES taxonomy code prefix matches provider type |
| `anchor` | Location narrowed to a confirmed center's address (phase 2) |
| `unique` | No close competitor in state — score gap ≥ 0.15 over next candidate |

**HIGH** confidence requires all applicable signals to fire. No records are auto-approved — every match requires human confirmation in the review UI.

## Setup

**Prerequisites**
- Python 3.10+
- NPPES bulk data download from [CMS](https://download.cms.gov/nppes/NPI_Files.html) (~8 GB)

**Install dependencies**
```bash
pip install -r requirements.txt
```

**Build the local database (one-time, ~20–30 min)**
```bash
python pipeline/build_local_db.py
```
Produces `data/nppes_local.db` (~2.7 GB).

**Run matching**
```bash
python pipeline/match_medical_centers.py
python pipeline/match_medical_professionals.py

# After approving centers in the review UI:
python pipeline/match_professionals_phase2.py
```

**Start the review site**
```bash
python review_site/app.py
# Open http://localhost:5001
```

## Data

Input files and the NPPES database are not included in this repository. Place them in `data/` before running:

| File | Description |
|------|-------------|
| `data/nppes_local.db` | Built by `build_local_db.py` from the NPPES bulk download |
| `data/campaign_medicalcenter.csv` | HHL facility records |
| `data/campaign_medicalprofessional_enriched.csv` | HHL provider records |

## Project structure

```
pipeline/
  build_local_db.py                  # NPPES CSV → SQLite
  match_medical_centers.py           # Facility matching (phase 1)
  match_medical_professionals.py     # Individual matching (phase 1)
  match_professionals_phase2.py      # Individual matching (phase 2, center-anchored)
  nicknames.py                       # First-name expansion (Bob → Robert, etc.)
review_site/
  app.py                             # Flask application
  templates/                         # Jinja2 templates
  requirements.txt
requirements.txt
```

## Key design decisions

**Local SQLite over live API** — The NPPES API has rate limits and latency that make bulk matching impractical. Loading the full dataset locally enables state-level bulk queries in milliseconds rather than per-record API calls.

**Two-phase professional matching** — Professionals share common names and cities, making state-wide fuzzy matching imprecise. Phase 2 narrows the candidate pool to providers registered at a confirmed center's address, reducing false positives significantly.

**No auto-approvals** — Even HIGH confidence matches require human confirmation. The cost of a false positive (incorrect NPI on a provider record) is high enough that the tool is designed to assist reviewers, not replace them.

**Nickname expansion** — NPPES uses legal names; client records often use informal names (Bill, Bob, Liz). `nicknames.py` expands query names to cover common variants before scoring, preventing missed matches on name alone.
