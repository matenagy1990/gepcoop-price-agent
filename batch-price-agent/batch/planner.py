"""
Mapping előellenőrzés és futási terv összeállítása.
Nem indít scrapert — csak a Supabase article_mapping táblát kérdezi le.
"""
import logging
from shared.supabase_client import get_supabase
from shared.supplier_registry import SUPPLIERS, get_supplier_name

log = logging.getLogger(__name__)

_IMPLEMENTED = set(SUPPLIERS.keys())


def _lookup_rows(part_numbers: list[str]) -> dict[str, dict]:
    """Egyetlen kötegelt lekérdezéssel betölti az összes cikkszámhoz tartozó
    article_mapping sort.

    A korábbi megoldás cikkszámonként külön ``.ilike`` query-t futtatott (N+1),
    ami 100 cikknél ~7 mp volt. Ez egyetlen ``.in_()`` lekérdezésre cseréli
    (~0,1–0,3 mp): az ``.in_()`` egyenlőség-szűrő, ezért használja a
    gepcoop_part_no indexet (az ``ilike`` nem tudná → szekvenciális pásztázás
    lenne, ~5 mp).

    A kis-/nagybetű-függetlenség megőrzéséhez minden cikkszámot három alakban
    keresünk: eredeti, csupa nagy, csupa kicsi. Ez a tábla minden „csak nagy"
    vagy „csak kicsi" betűs értékére pontosan megegyezik a régi ``ilike``
    eredménnyel; a néhány vegyes betűs (intra-mixed) cikknél a pontos beírás
    továbbra is talál. A párosítás Python-oldalon a NAGYBETŰS alak alapján
    történik.

    A visszaadott dict kulcsa a cikkszám NAGYBETŰS alakja → a hozzá tartozó sor.
    Nincs találat esetén a kulcs egyszerűen hiányzik a dict-ből.
    """
    cleaned = [pn.strip() for pn in part_numbers if pn and pn.strip()]
    if not cleaned:
        return {}
    sb = get_supabase()
    variants = list({v for pn in cleaned for v in (pn, pn.upper(), pn.lower())})
    rows = (
        sb.table("article_mapping")
        .select("*")
        .in_("gepcoop_part_no", variants)
        .execute()
        .data
        or []
    )
    return {(r.get("gepcoop_part_no") or "").upper(): r for r in rows}


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

    # Egyetlen kötegelt lekérdezés az összes cikkszámra (N+1 helyett 1 query).
    rows_by_part = _lookup_rows(part_numbers)

    items = []
    searchable_count = 0
    missing_mapping_count = 0

    for pn in part_numbers:
        row = rows_by_part.get(pn.strip().upper())
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
