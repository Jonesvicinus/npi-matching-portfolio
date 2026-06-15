#!/usr/bin/env python3
"""
Build data/nppes_local.db from local NPPES bulk CSV files.

Usage:
    python3 build_local_db.py            # full build (~15-20 min)
    python3 build_local_db.py --test     # first 50k rows only (~30 sec, for testing)

If interrupted, re-run the same command — it will resume from the last checkpoint.
"""

import argparse
import csv
import itertools
import json
import os
import sqlite3
from datetime import datetime

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(BASE_DIR, "data")
NPPES_DIR       = os.path.join(BASE_DIR, "NPPES_Data_Dissemination_May_2026_V2")
MAIN_CSV        = os.path.join(NPPES_DIR, "npidata_pfile_20050523-20260510.csv")
OTHER_CSV       = os.path.join(NPPES_DIR, "othername_pfile_20050523-20260510.csv")
PL_CSV          = os.path.join(NPPES_DIR, "pl_pfile_20050523-20260510.csv")
EP_CSV          = os.path.join(NPPES_DIR, "endpoint_pfile_20050523-20260510.csv")
DB_PATH         = os.path.join(DATA_DIR, "nppes_local.db")
CHECKPOINT_FILE = os.path.join(DATA_DIR, "build_checkpoint.json")

BATCH_SIZE      = 10_000
TEST_LIMIT      = 50_000


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return None


def save_checkpoint(data):
    data["saved_at"] = datetime.now().isoformat()
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            npi               TEXT PRIMARY KEY,
            entity_type       INTEGER,
            org_name          TEXT,
            last_name         TEXT,
            first_name        TEXT,
            credential        TEXT,
            practice_address1 TEXT,
            practice_city     TEXT,
            practice_state    TEXT,
            practice_zip      TEXT,
            practice_phone    TEXT,
            deactivation_date TEXT,
            parent_org_name   TEXT,
            enumeration_date  TEXT,
            last_update_date  TEXT,
            npi_type          TEXT,
            is_sole_proprietor TEXT,
            mailing_address1  TEXT,
            mailing_city      TEXT,
            mailing_state     TEXT,
            mailing_zip       TEXT,
            mailing_phone     TEXT
        );

        CREATE TABLE IF NOT EXISTS taxonomies (
            npi           TEXT,
            taxonomy_code TEXT,
            is_primary    INTEGER
        );

        CREATE TABLE IF NOT EXISTS other_names (
            npi        TEXT,
            other_name TEXT,
            name_type  TEXT
        );

        CREATE TABLE IF NOT EXISTS practice_locations (
            npi      TEXT,
            address1 TEXT,
            address2 TEXT,
            city     TEXT,
            state    TEXT,
            zip      TEXT,
            phone    TEXT
        );

        CREATE TABLE IF NOT EXISTS endpoints (
            npi              TEXT,
            endpoint_type    TEXT,
            endpoint         TEXT,
            affiliation      TEXT,
            description      TEXT,
            org_name         TEXT,
            use_code         TEXT,
            use_description  TEXT,
            content_type     TEXT,
            address1         TEXT,
            city             TEXT,
            state            TEXT,
            postal_code      TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 1: providers + taxonomies
# ---------------------------------------------------------------------------

def build_providers(conn, checkpoint, limit=None):
    rows_already_read = checkpoint.get("providers_rows_read", 0) if checkpoint else 0
    total             = checkpoint.get("providers_inserted", 0) if checkpoint else 0
    skipped           = checkpoint.get("providers_skipped", 0) if checkpoint else 0

    if rows_already_read:
        print(f"  Resuming from row {rows_already_read:,} ({total:,} inserted, {skipped:,} deactivated skipped)...")
    else:
        print(f"  Reading main CSV {'(first {:,} rows)'.format(limit) if limit else '(full file, ~9.5M rows)'}...")

    prov_batch  = []
    tax_batch   = []
    rows_read   = 0

    with open(MAIN_CSV, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)

        # Fast-skip already-processed rows
        if rows_already_read:
            print(f"  Fast-skipping {rows_already_read:,} rows already processed...")
            for _ in itertools.islice(reader, rows_already_read):
                pass
            rows_read = rows_already_read

        for row in reader:
            rows_read += 1

            if limit and rows_read > limit:
                break

            deact = row.get("NPI Deactivation Date", "").strip()
            if deact:
                skipped += 1
            else:
                npi         = row["NPI"].strip()
                entity_type = row["Entity Type Code"].strip()

                prov_batch.append((
                    npi,
                    int(entity_type) if entity_type else None,
                    row.get("Provider Organization Name (Legal Business Name)", "").strip() or None,
                    row.get("Provider Last Name (Legal Name)", "").strip() or None,
                    row.get("Provider First Name", "").strip() or None,
                    row.get("Provider Credential Text", "").strip() or None,
                    row.get("Provider First Line Business Practice Location Address", "").strip() or None,
                    row.get("Provider Business Practice Location Address City Name", "").strip() or None,
                    row.get("Provider Business Practice Location Address State Name", "").strip() or None,
                    row.get("Provider Business Practice Location Address Postal Code", "").strip() or None,
                    row.get("Provider Business Practice Location Address Telephone Number", "").strip() or None,
                    deact or None,
                    row.get("Parent Organization LBN", "").strip() or None,
                    row.get("Provider Enumeration Date", "").strip() or None,
                    row.get("Last Update Date", "").strip() or None,
                    entity_type or None,
                    row.get("Is Sole Proprietor", "").strip() or None,
                    row.get("Provider First Line Business Mailing Address", "").strip() or None,
                    row.get("Provider Business Mailing Address City Name", "").strip() or None,
                    row.get("Provider Business Mailing Address State Name", "").strip() or None,
                    row.get("Provider Business Mailing Address Postal Code", "").strip() or None,
                    row.get("Provider Business Mailing Address Telephone Number", "").strip() or None,
                ))

                for i in range(1, 16):
                    code    = row.get(f"Healthcare Provider Taxonomy Code_{i}", "").strip()
                    primary = row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}", "").strip()
                    if code:
                        tax_batch.append((npi, code, 1 if primary == "Y" else 0))

                total += 1

            if rows_read % BATCH_SIZE == 0:
                conn.executemany(
                    "INSERT OR IGNORE INTO providers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    prov_batch,
                )
                conn.executemany("INSERT INTO taxonomies VALUES (?,?,?)", tax_batch)
                conn.commit()
                prov_batch.clear()
                tax_batch.clear()

                save_checkpoint({
                    "stage":               "providers",
                    "providers_rows_read": rows_read,
                    "providers_inserted":  total,
                    "providers_skipped":   skipped,
                })

            if rows_read % 500_000 == 0:
                print(f"  {rows_read:,} rows read — {total:,} active, {skipped:,} deactivated...", flush=True)

    if prov_batch:
        conn.executemany(
            "INSERT OR IGNORE INTO providers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            prov_batch,
        )
        conn.executemany("INSERT INTO taxonomies VALUES (?,?,?)", tax_batch)
        conn.commit()

    print(f"  Done — {total:,} active providers inserted ({skipped:,} deactivated skipped).")
    return total


# ---------------------------------------------------------------------------
# Stage 2-4: other_names, practice_locations, endpoints
# ---------------------------------------------------------------------------

def build_other_names(conn):
    print("  Reading othername CSV (47MB)...")
    batch = []
    count = 0
    with open(OTHER_CSV, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            npi  = row["NPI"].strip()
            name = row.get("Provider Other Organization Name", "").strip()
            typ  = row.get("Provider Other Organization Name Type Code", "").strip()
            if npi and name:
                batch.append((npi, name, typ))
                count += 1
                if count % BATCH_SIZE == 0:
                    conn.executemany("INSERT INTO other_names VALUES (?,?,?)", batch)
                    conn.commit()
                    batch.clear()
    if batch:
        conn.executemany("INSERT INTO other_names VALUES (?,?,?)", batch)
        conn.commit()
    print(f"  Done — {count:,} alternate names.")


def build_practice_locations(conn):
    print("  Reading secondary locations CSV (109MB)...")
    batch = []
    count = 0
    with open(PL_CSV, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            npi = row["NPI"].strip()
            if not npi:
                continue
            batch.append((
                npi,
                row.get("Provider Secondary Practice Location Address- Address Line 1", "").strip() or None,
                row.get("Provider Secondary Practice Location Address-  Address Line 2", "").strip() or None,
                row.get("Provider Secondary Practice Location Address - City Name", "").strip() or None,
                row.get("Provider Secondary Practice Location Address - State Name", "").strip() or None,
                row.get("Provider Secondary Practice Location Address - Postal Code", "").strip() or None,
                row.get("Provider Secondary Practice Location Address - Telephone Number", "").strip() or None,
            ))
            count += 1
            if count % BATCH_SIZE == 0:
                conn.executemany("INSERT INTO practice_locations VALUES (?,?,?,?,?,?,?)", batch)
                conn.commit()
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO practice_locations VALUES (?,?,?,?,?,?,?)", batch)
        conn.commit()
    print(f"  Done — {count:,} secondary locations.")


def build_endpoints(conn):
    print("  Reading HIE endpoints CSV (118MB)...")
    batch = []
    count = 0
    with open(EP_CSV, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            npi = row["NPI"].strip()
            if not npi:
                continue
            batch.append((
                npi,
                row.get("Endpoint Type", "").strip() or None,
                row.get("Endpoint", "").strip() or None,
                row.get("Affiliation", "").strip() or None,
                row.get("Endpoint Description", "").strip() or None,
                row.get("Affiliation Legal Business Name", "").strip() or None,
                row.get("Use Code", "").strip() or None,
                row.get("Use Description", "").strip() or None,
                row.get("Content Type", "").strip() or None,
                row.get("Affiliation Address Line One", "").strip() or None,
                row.get("Affiliation Address City", "").strip() or None,
                row.get("Affiliation Address State", "").strip() or None,
                row.get("Affiliation Address Postal Code", "").strip() or None,
            ))
            count += 1
            if count % BATCH_SIZE == 0:
                conn.executemany("INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                conn.commit()
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    print(f"  Done — {count:,} HIE endpoints.")


def create_indexes(conn):
    print("  Creating indexes...")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_providers_state_type
            ON providers (practice_state, entity_type);
        CREATE INDEX IF NOT EXISTS idx_providers_lastname
            ON providers (last_name, entity_type);
        CREATE INDEX IF NOT EXISTS idx_taxonomies_npi
            ON taxonomies (npi);
        CREATE INDEX IF NOT EXISTS idx_other_names_npi
            ON other_names (npi);
        CREATE INDEX IF NOT EXISTS idx_practice_locations_npi
            ON practice_locations (npi);
        CREATE INDEX IF NOT EXISTS idx_endpoints_npi
            ON endpoints (npi);
    """)
    print("  Done.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help=f"Quick test: load only first {TEST_LIMIT:,} rows")
    args = parser.parse_args()

    limit = TEST_LIMIT if args.test else None

    if args.test:
        print(f"TEST MODE — loading first {TEST_LIMIT:,} rows only (~30 seconds)")
    else:
        print("FULL BUILD — loading all ~9.5M rows (~15-20 minutes)")

    checkpoint = load_checkpoint()
    if checkpoint:
        stage = checkpoint.get("stage", "providers")
        rows  = checkpoint.get("providers_rows_read", 0)
        print(f"\nCheckpoint found (stage={stage}, rows_read={rows:,}) — resuming.\n")
    else:
        print()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-256000")

    try:
        stage = checkpoint.get("stage", "providers") if checkpoint else "providers"

        # Stage 1: providers
        if stage == "providers":
            print("[1/5] Providers + Taxonomies")
            create_schema(conn)
            build_providers(conn, checkpoint, limit=limit)
            save_checkpoint({"stage": "other_names"})

        # Stage 2: other names (skip for test mode — too slow relative to value)
        if not args.test:
            stage = load_checkpoint().get("stage") if load_checkpoint() else "other_names"
            if stage in ("other_names",):
                print("\n[2/5] Other Names")
                build_other_names(conn)
                save_checkpoint({"stage": "practice_locations"})

            stage = load_checkpoint().get("stage") if load_checkpoint() else "practice_locations"
            if stage in ("practice_locations",):
                print("\n[3/5] Secondary Practice Locations")
                build_practice_locations(conn)
                save_checkpoint({"stage": "endpoints"})

            stage = load_checkpoint().get("stage") if load_checkpoint() else "endpoints"
            if stage in ("endpoints",):
                print("\n[4/5] HIE Endpoints")
                build_endpoints(conn)
                save_checkpoint({"stage": "indexes"})
        else:
            print("\n[2-4/5] Skipping other_names / locations / endpoints in test mode")

        print("\n[5/5] Indexes")
        create_indexes(conn)

        clear_checkpoint()

    except KeyboardInterrupt:
        print("\n\nInterrupted — progress saved. Re-run the same command to resume.")
        conn.close()
        return
    finally:
        conn.close()

    size_mb = os.path.getsize(DB_PATH) / 1_048_576
    mode    = "test" if args.test else "full"
    print(f"\nDone ({mode}) — {DB_PATH} ({size_mb:.0f} MB)")
    if args.test:
        print("\nTest DB ready. Run the full pipeline:")
        print("  python3 match_medical_centers.py")
        print("  python3 match_medical_professionals.py")
        print("  cd review_site && python3 app.py")
        print("\nWhen happy, run the full build:")
        print("  python3 build_local_db.py")


if __name__ == "__main__":
    main()
