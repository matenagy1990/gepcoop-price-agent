import asyncio
import csv
import json
import logging
import re
import os
import time
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# 1-hour module-level cache for the CZK→HUF exchange rate
_fx_cache: dict = {}
_FX_TTL = 3600

MAPPING_FILE = Path(__file__).parent.parent / "assets" / "mapping.csv"
USER_FILE    = Path(__file__).parent.parent / "assets" / "user.csv"


# ---------------------------------------------------------------------------
# Price / stock parsing helpers
# ---------------------------------------------------------------------------

def parse_price_string(price_str: str) -> tuple[float, int, str]:
    """
    Parse supplier price strings into (raw_price, unit_qty, unit).

    Examples:
      "6,76 Ft/db"           → (6.76, 1, "db")
      "249,60 Ft / 1.000 db" → (249.60, 1000, "db")

    Hungarian number format: '.' = thousands separator, ',' = decimal separator.
    """
    if not price_str:
        raise ValueError("Empty price string")

    parts = price_str.split("/")
    if len(parts) != 2:
        raise ValueError(f"Cannot parse price format (no '/' found): '{price_str}'")

    price_part = re.sub(r"[A-Za-z\s]", "", parts[0])
    price_part = price_part.replace(".", "").replace(",", ".")
    raw_price = float(price_part)

    unit_part  = parts[1].strip()
    unit_match = re.match(r"^([0-9.,\s]*)?\s*(\w+)$", unit_part)
    if not unit_match:
        raise ValueError(f"Cannot parse unit part: '{unit_part}'")

    qty_str = (unit_match.group(1) or "").strip()
    unit    = unit_match.group(2).strip()

    if qty_str:
        qty_clean = qty_str.replace(".", "").replace(",", "").replace(" ", "")
        unit_qty = int(qty_clean)
    else:
        unit_qty = 1

    return raw_price, unit_qty, unit


def parse_stock_string(s: str) -> int:
    """
    Extract stock number from strings like:
      'Budapest: 20 371 db'  → 20371
      '114.000 db'           → 114000
    """
    if not s:
        return 0
    digit_groups = re.findall(r"\d+", s)
    return int("".join(digit_groups)) if digit_groups else 0


# ---------------------------------------------------------------------------
# user.csv helpers  — read supplier metadata (URL, credentials)
# ---------------------------------------------------------------------------

def _read_user_csv() -> dict[str, dict]:
    """
    Parse assets/user.csv into {supplier_id: {field: value}}.

    CSV layout (transposed):
      field,       csavarda,               irontrade,          koelner,  mekrs
      weboldal,    https://csavarda.hu/,   https://irontrade.hu/, ...
      felhasznalonev, gepcoop@gepcoop.hu,  ...
      jelszo,      Din1990_gepcoop,        ...
    """
    result: dict[str, dict] = {}
    with open(USER_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = row["field"].strip().lower()
            for supplier, value in row.items():
                if supplier == "field":
                    continue
                sid = supplier.strip().lower()
                if sid not in result:
                    result[sid] = {}
                result[sid][field] = value.strip()
    return result


def get_supplier_info() -> dict[str, dict]:
    """Return supplier metadata keyed by supplier_id."""
    return _read_user_csv()


# ---------------------------------------------------------------------------
# Tool 1: lookup_mapping_all  — returns every supplier for a part number
#         lookup_mapping       — returns first supplier (backward compat)
#
# mapping.csv is a wide table:
#   gepcoop_part_no | csavarda_part_no | irontrade_part_no | koelner_part_no | mekrs_part_no
# Column names follow the pattern  {supplier_id}_part_no
# ---------------------------------------------------------------------------

async def _get_czk_huf_rate() -> float | None:
    """open.er-api.com (free, no key) → CZK/HUF rate. Cached for 1 hour."""
    if _fx_cache and time.time() - _fx_cache.get("ts", 0) < _FX_TTL:
        return _fx_cache["rate"]

    def _fetch():
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/CZK",
            headers={"User-Agent": "gepcoop-price-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    try:
        data = await asyncio.to_thread(_fetch)
        rate = float(data["rates"]["HUF"])
        _fx_cache.update({"rate": rate, "ts": time.time()})
        updated = data.get("time_last_update_utc", "?")[:16]
        log.info(f"CZK→HUF árfolyam: {rate} (open.er-api.com, {updated})")
        return rate
    except Exception as exc:
        log.warning(f"CZK→HUF árfolyam lekérése sikertelen: {exc}")
        return None


def lookup_mapping_all(internal_part_no: str) -> list[dict]:
    """
    Return a list of supplier entries for the given Gép-Coop part number.
    Each entry: {supplier_id, supplier_part_no, supplier_url}
    Skips any supplier column that is empty.
    """
    search = internal_part_no.strip().upper()
    supplier_info = get_supplier_info()

    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["gepcoop_part_no"].strip().upper() != search:
                continue
            results = []
            for col, val in row.items():
                if col == "gepcoop_part_no":
                    continue
                if not col.endswith("_part_no"):
                    continue
                val = val.strip()
                if not val:
                    continue
                supplier_id = col[: -len("_part_no")]          # e.g. "csavarda"
                url = supplier_info.get(supplier_id, {}).get("weboldal", "")
                results.append({
                    "supplier_id":      supplier_id,
                    "supplier_part_no": val,
                    "supplier_url":     url,
                })
            return results

    return []


def get_all_part_numbers() -> list[str]:
    """Return every Gép-Coop internal part number in the mapping file."""
    parts = []
    try:
        with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pn = row.get("gepcoop_part_no", "").strip()
                if pn:
                    parts.append(pn)
    except FileNotFoundError:
        pass
    return parts


def lookup_mapping(internal_part_no: str) -> dict:
    """Return the first supplier entry (backward compatibility)."""
    results = lookup_mapping_all(internal_part_no)
    if not results:
        raise ValueError(
            f"Part number '{internal_part_no}' was not found in the supplier mapping."
        )
    return results[0]


# ---------------------------------------------------------------------------
# Tool 2: fetch_supplier_price
# ---------------------------------------------------------------------------

# Suppliers that have a working Playwright scraper
_IMPLEMENTED_SUPPLIERS = {"csavarda", "irontrade", "koelner", "mekrs"}


async def fetch_supplier_price(supplier_id: str, supplier_part_no: str, on_progress=None) -> dict:
    """
    Fetch current price and stock from the supplier website, then normalise
    the price to per-db so results from different suppliers are comparable.

    Raises ValueError for suppliers without a scraper yet.
    """
    supplier_id = supplier_id.strip().lower()

    if supplier_id == "csavarda":
        from browser.supplier_csavarda import fetch_price
    elif supplier_id == "irontrade":
        from browser.supplier_irontrade import fetch_price
    elif supplier_id == "koelner":
        from browser.supplier_koelner import fetch_price
    elif supplier_id == "mekrs":
        from browser.supplier_mekrs import fetch_price
    else:
        raise ValueError(
            f"Supplier '{supplier_id}' does not have a browser script yet. "
            f"Create browser/supplier_{supplier_id}.py to enable it."
        )

    raw = await fetch_price(supplier_part_no, on_progress=on_progress)

    # Normalise: price per 1 db
    raw["price_per_db"] = round(raw["price_raw"] / raw["price_unit_qty"], 6)

    # For CZK results, add a HUF-comparable price via ECB live rate
    if raw.get("currency") == "CZK":
        rate = await _get_czk_huf_rate()
        if rate is not None:
            raw["price_per_db_huf"] = round(raw["price_per_db"] * rate, 6)
            raw["czk_huf_rate"]     = round(rate, 4)

    return raw
