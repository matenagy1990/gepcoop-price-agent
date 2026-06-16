"""
Eredmény aggregáció: cikkszámonkénti mátrix, TOP és ALT jelölés.
"""
from shared.supplier_registry import get_supplier_name, get_supplier_url, build_product_link


def build_matrix(
    preview_items: list[dict],
    scrape_results: list[dict],
    selected_suppliers: list[str],
) -> dict:
    """
    Felépíti az eredménymátrixot és meghatározza a TOP/ALT ajánlatot.

    Visszatér:
    {
        "items": [
            {
                "gepcoop_part_no": str,
                "product_name": str | None,
                "suppliers": {
                    supplier_id: {result_dict with is_top, is_alternative}
                },
                "top_supplier": str | None,
                "top_price_huf": float | None,
                "alt_supplier": str | None,
                "alt_price_huf": float | None,
                "result_count": int,
                "error_count": int,
            }
        ],
        "selected_suppliers": [...],
        "supplier_names": {...},
    }
    """
    # Csoportosítás: gepcoop_part_no → supplier_id → result
    result_map: dict[str, dict[str, dict]] = {}
    for r in scrape_results:
        pn = r["gepcoop_part_no"]
        sid = r["supplier_id"]
        result_map.setdefault(pn, {})[sid] = r

    items_out = []

    for item in preview_items:
        pn = item["gepcoop_part_no"]
        product_name = item.get("product_name")
        supplier_cells: dict[str, dict] = {}

        # Minden kiválasztott supplier cellájának felépítése
        for sid in selected_suppliers:
            mapping_info = item["suppliers"].get(sid, {})
            mapping_status = mapping_info.get("status", "missing_mapping")

            if mapping_status != "mapped":
                supplier_cells[sid] = {
                    "mapping_status": mapping_status,
                    "scrape_status": "skipped",
                    "supplier_part_no": None,
                    "raw_price": None,
                    "currency": None,
                    "unit": None,
                    "price_unit_qty": None,
                    "normalized_price_huf": None,
                    "stock_quantity": None,
                    "stock_raw": None,
                    "product_url": None,
                    "error_message": None,
                    "is_top": False,
                    "is_alternative": False,
                    "label": "no_mapping",
                }
            else:
                scraped = result_map.get(pn, {}).get(sid)
                if scraped is None:
                    supplier_cells[sid] = {
                        "mapping_status": "mapped",
                        "scrape_status": "pending",
                        "supplier_part_no": mapping_info.get("supplier_part_no"),
                        "raw_price": None,
                        "currency": None,
                        "unit": None,
                        "price_unit_qty": None,
                        "normalized_price_huf": None,
                        "stock_quantity": None,
                        "stock_raw": None,
                        "product_url": None,
                        "error_message": None,
                        "is_top": False,
                        "is_alternative": False,
                        "label": "pending",
                    }
                else:
                    supplier_cells[sid] = {
                        "mapping_status": "mapped",
                        "scrape_status": scraped["scrape_status"],
                        "supplier_part_no": scraped.get("supplier_part_no"),
                        "raw_price": scraped.get("raw_price"),
                        "currency": scraped.get("currency"),
                        "unit": scraped.get("unit"),
                        "price_unit_qty": scraped.get("price_unit_qty"),
                        "normalized_price_huf": scraped.get("normalized_price_huf"),
                        "stock_quantity": scraped.get("stock_quantity"),
                        "stock_raw": scraped.get("stock_raw"),
                        "product_url": build_product_link(
                            sid,
                            scraped.get("supplier_part_no"),
                            scraped.get("product_url"),
                        ),
                        "error_message": scraped.get("error_message"),
                        "is_top": False,
                        "is_alternative": False,
                        "label": _cell_label(scraped),
                    }

        # TOP és ALT meghatározása ár alapján
        priced = [
            (sid, cell["normalized_price_huf"])
            for sid, cell in supplier_cells.items()
            if cell.get("normalized_price_huf") is not None
        ]
        priced.sort(key=lambda x: x[1])

        top_supplier = top_price = alt_supplier = alt_price = None

        if len(priced) >= 1:
            top_supplier, top_price = priced[0]
            supplier_cells[top_supplier]["is_top"] = True
            supplier_cells[top_supplier]["label"] = "top"

        if len(priced) >= 2:
            alt_supplier, alt_price = priced[1]
            supplier_cells[alt_supplier]["is_alternative"] = True
            supplier_cells[alt_supplier]["label"] = "alternative"

        result_count = sum(1 for c in supplier_cells.values() if c["scrape_status"] == "ok")
        error_count = sum(
            1 for c in supplier_cells.values()
            if c["scrape_status"] not in ("ok", "skipped", "pending")
        )

        items_out.append({
            "gepcoop_part_no": pn,
            "product_name": product_name,
            "suppliers": supplier_cells,
            "top_supplier": top_supplier,
            "top_price_huf": top_price,
            "alt_supplier": alt_supplier,
            "alt_price_huf": alt_price,
            "result_count": result_count,
            "error_count": error_count,
        })

    return {
        "items": items_out,
        "selected_suppliers": selected_suppliers,
        "supplier_names": {sid: get_supplier_name(sid) for sid in selected_suppliers},
        "supplier_urls": {sid: get_supplier_url(sid) for sid in selected_suppliers},
    }


def _cell_label(scraped: dict) -> str:
    status = scraped.get("scrape_status", "error")
    if status == "ok":
        return "ok"
    if status == "not_found":
        return "not_found"
    if status == "not_priced":
        return "not_priced"
    if status == "timeout":
        return "timeout"
    if status == "login_failed":
        return "login_failed"
    return "error"
