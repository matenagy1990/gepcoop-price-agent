import asyncio
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED

load_dotenv()

log = logging.getLogger("argip")

_SUPABASE = None
TABLE = "argip_price_list"
SELECT_COLS = (
    "ean_code,argip_part_no,customer_part_no,description_en,description_pl,"
    "customer_description,base_price_eur,price_lvl_1_eur,moq_lvl_1_pcs,"
    "price_lvl_2_eur,moq_lvl_2_pcs,stock_pcs,box_quantity_pcs,sku"
)


def _get_supabase():
    global _SUPABASE
    if _SUPABASE is not None:
        return _SUPABASE
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("Supabase not configured (SUPABASE_URL / SUPABASE_KEY missing).")
    from supabase import create_client
    _SUPABASE = create_client(url, key)
    return _SUPABASE


def _query_row(part_no: str) -> dict | None:
    """Search by argip_part_no first, then by ean_code (EAN codes come from the mapping)."""
    sb = _get_supabase()
    res = (
        sb.table(TABLE)
        .select(SELECT_COLS)
        .ilike("argip_part_no", part_no)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if row:
        return row
    # Fallback: the mapping may store an EAN code instead of the Argip part number.
    res2 = (
        sb.table(TABLE)
        .select(SELECT_COLS)
        .ilike("ean_code", part_no)
        .limit(1)
        .execute()
    )
    return (res2.data or [None])[0]


async def fetch_price(supplier_part_no: str, on_progress=None) -> dict:
    async def emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            await on_progress({"step": "lookup", "status": "running", "msg": msg})

    part_no = (supplier_part_no or "").strip()
    await emit(f"Looking up {part_no} in Argip price list…")
    row = _query_row(part_no)
    if not row:
        raise RuntimeError(MSG_NOT_FOUND)

    base_price = row.get("base_price_eur")
    if base_price in (None, ""):
        raise RuntimeError(MSG_NOT_PRICED)

    description = (
        row.get("customer_description")
        or row.get("description_en")
        or row.get("customer_part_no")
        or part_no
    )
    stock = row.get("stock_pcs")
    log.info("Argip parsed — part=%s, base_price=%s EUR/db, stock=%s", part_no, base_price, stock)
    return {
        "supplier_part_no": part_no,
        "product_name": description,
        "price_raw": float(base_price),
        "price_unit_qty": 1,
        "currency": "EUR",
        "unit": "db",
        "stock": int(stock) if stock is not None else 0,
        "argip_base_price_eur": float(base_price),
        "argip_price_lvl_1_eur": float(row["price_lvl_1_eur"]) if row.get("price_lvl_1_eur") not in (None, "") else None,
        "argip_moq_lvl_1_pcs": int(row["moq_lvl_1_pcs"]) if row.get("moq_lvl_1_pcs") not in (None, "") else None,
        "argip_price_lvl_2_eur": float(row["price_lvl_2_eur"]) if row.get("price_lvl_2_eur") not in (None, "") else None,
        "argip_moq_lvl_2_pcs": int(row["moq_lvl_2_pcs"]) if row.get("moq_lvl_2_pcs") not in (None, "") else None,
        "queried_at": datetime.now().isoformat(timespec="seconds"),
    }
