"""
One-time migration script: uploads assets/mapping.csv → Supabase article_mapping table.

Usage:
    python scripts/import_mapping_to_supabase.py

Prerequisites:
    1. Create the table in Supabase SQL Editor:
       CREATE TABLE IF NOT EXISTS article_mapping (
           gepcoop_part_no   TEXT PRIMARY KEY,
           name              TEXT,
           csavarda_part_no  TEXT,
           irontrade_part_no TEXT,
           koelner_part_no   TEXT,
           mekrs_part_no     TEXT,
           fabory_part_no    TEXT,
           ferdinand_part_no TEXT,
           reyher_part_no    TEXT,
           hopefix_part_no   TEXT,
           fastbolt_part_no  TEXT,
           schaefer_part_no  TEXT,
           kingb2b_part_no   TEXT,
           wasishop_part_no  TEXT
       );
    2. SUPABASE_URL and SUPABASE_KEY must be set in .env
"""

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MAPPING_FILE = Path(__file__).parent.parent / "assets" / "mapping.csv"
BATCH_SIZE   = 500
TABLE        = "article_mapping"

EMPTY_VALUES = {"", "-", "–", "—", "N/A", "n/a"}


def clean(val: str) -> str | None:
    v = val.strip()
    return None if v in EMPTY_VALUES else v


def load_rows() -> list[dict]:
    rows = []
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            part_no = clean(row.get("gepcoop_part_no", ""))
            if not part_no:
                continue
            rows.append({
                "gepcoop_part_no":   part_no,
                "name":              clean(row.get("name", "")),
                "csavarda_part_no":  clean(row.get("csavarda_part_no", "")),
                "irontrade_part_no": clean(row.get("irontrade_part_no", "")),
                "koelner_part_no":   clean(row.get("koelner_part_no", "")),
                "mekrs_part_no":     clean(row.get("mekrs_part_no", "")),
                "fabory_part_no":    clean(row.get("fabory_part_no", "")),
                "ferdinand_part_no": clean(row.get("ferdinand_part_no", "")),
                "reyher_part_no":    clean(row.get("reyher_part_no", "")),
                "hopefix_part_no":   clean(row.get("hopefix_part_no", "")),
                "fastbolt_part_no":  clean(row.get("fastbolt_part_no", "")),
                "schaefer_part_no":  clean(row.get("schaefer_part_no", "")),
                "kingb2b_part_no":   clean(row.get("kingb2b_part_no", "")),
                "wasishop_part_no":  clean(row.get("wasishop_part_no", "")),
            })
    return rows


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        sys.exit(1)

    print(f"Connecting to Supabase: {SUPABASE_URL}")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    print(f"Loading {MAPPING_FILE}…")
    rows = load_rows()
    print(f"  {len(rows)} rows loaded")

    total   = len(rows)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    inserted = 0

    for i in range(batches):
        batch = rows[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
        result = (
            client.table(TABLE)
            .upsert(batch, on_conflict="gepcoop_part_no")
            .execute()
        )
        inserted += len(batch)
        pct = inserted / total * 100
        print(f"  Batch {i+1}/{batches} — {inserted}/{total} rows ({pct:.0f}%)")

    print(f"\nDone. {inserted} rows upserted into '{TABLE}'.")


if __name__ == "__main__":
    main()
