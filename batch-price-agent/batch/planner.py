"""
Mapping előellenőrzés és futási terv összeállítása.
Nem indít scrapert — csak a Supabase article_mapping táblát kérdezi le.
"""
import logging
from shared.supabase_client import get_supabase
from shared.supplier_registry import SUPPLIERS, get_supplier_name

log = logging.getLogger(__name__)

_IMPLEMENTED = set(SUPPLIERS.keys())


def _lookup_row(gepcoop_part_no: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("article_mapping")
        .select("*")
        .ilike("gepcoop_part_no", gepcoop_part_no.strip())
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _row_to_supplier_map(row: dict, selected_suppliers: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for sid in selected_suppliers:
        col = f"{sid}_part_no"
        val = row.get(col)
        if val and val not in ("-", "–", "—", "N/A", "n/a", ""):
            result[sid] = {"status": "mapped", "supplier_part_no": str(val).strip()}
        else:
            result[sid] = {"status": "missing_mapping", "supplier_part_no": None}
    return result


def clean_part_numbers(raw_list: list[str]) -> tuple[list[str], list[str]]:
    """Returns (unique_clean_list, duplicate_list)."""
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    for p in raw_list:
        cleaned = p.strip()
        if not cleaned:
            continue
        upper = cleaned.upper()
        if upper in seen:
            duplicates.append(cleaned)
        else:
            seen.add(upper)
            unique.append(cleaned)
    return unique, duplicates


def build_preview(
    project_name: str,
    selected_suppliers: list[str],
    gepcoop_part_numbers: list[str],
) -> dict:
    """
    Mapping előellenőrzés minden cikkszámra.
    Visszaad egy preview dict-et a futási tervvel együtt.
    """
    valid_suppliers = [s for s in selected_suppliers if s in _IMPLEMENTED]
    part_numbers, duplicates = clean_part_numbers(gepcoop_part_numbers)

    items = []
    searchable_count = 0
    missing_mapping_count = 0

    for pn in part_numbers:
        row = _lookup_row(pn)
        if row is None:
            product_name = None
            supplier_map = {sid: {"status": "not_in_db", "supplier_part_no": None} for sid in valid_suppliers}
            item_status = "not_in_db"
        else:
            product_name = row.get("name") or row.get("product_name") or None
            supplier_map = _row_to_supplier_map(row, valid_suppliers)
            has_any = any(v["status"] == "mapped" for v in supplier_map.values())
            item_status = "searchable" if has_any else "no_mapping"

        mapped_here = sum(1 for v in supplier_map.values() if v["status"] == "mapped")
        missing_here = sum(1 for v in supplier_map.values() if v["status"] != "mapped")
        searchable_count += mapped_here
        missing_mapping_count += missing_here

        items.append({
            "gepcoop_part_no": pn,
            "product_name": product_name,
            "status": item_status,
            "suppliers": supplier_map,
        })

    total_possible = len(part_numbers) * len(valid_suppliers)

    return {
        "project_name": project_name,
        "total_items": len(part_numbers),
        "duplicates": duplicates,
        "selected_suppliers": valid_suppliers,
        "supplier_names": {sid: get_supplier_name(sid) for sid in valid_suppliers},
        "total_possible_searches": total_possible,
        "searchable_count": searchable_count,
        "missing_mapping_count": missing_mapping_count,
        "supplier_worker_count": len(valid_suppliers),
        "items": items,
    }
