import os
from urllib.parse import quote

SUPPLIERS: dict[str, dict] = {
    "csavarda":  {"name": "Csavarda",        "url": os.environ.get("SUPPLIER_A_URL", "https://csavarda.hu/")},
    "irontrade": {"name": "Iron Trade",      "url": os.environ.get("SUPPLIER_B_URL", "https://irontrade.hu/")},
    "koelner":   {"name": "Köelner",         "url": os.environ.get("SUPPLIER_C_URL", "https://webshop.koelner.hu/")},
    "mekrs":     {"name": "Mekrs",           "url": os.environ.get("SUPPLIER_D_URL", "https://eshop.mekrs.cz/en")},
    "fabory":    {"name": "Fabory",          "url": os.environ.get("SUPPLIER_E_URL", "https://www.fabory.com/hu")},
    "reyher":    {"name": "Reyher",          "url": os.environ.get("SUPPLIER_F_URL", "https://rio.reyher.de")},
    "hopefix":   {"name": "Hopefix",         "url": os.environ.get("SUPPLIER_G_URL", "https://www.hopefix.cz/en")},
    "fastbolt":  {"name": "Fastbolt",        "url": os.environ.get("SUPPLIER_H_URL", "https://fbonline.fastbolt.com")},
    "schaefer":  {"name": "Schäfer Peters",  "url": os.environ.get("SUPPLIER_I_URL", "https://shop.schaefer-peters.com/b2b/en/")},
    "kingb2b":   {"name": "King B2B",        "url": os.environ.get("SUPPLIER_J_URL", "https://kingb2b.it/PORTAL/")},
    "wasishop":  {"name": "Wasishop",        "url": os.environ.get("SUPPLIER_K_URL", "https://www.wasishop.de")},
    "argip":     {"name": "Argip",           "url": ""},
    "inoxmare":  {"name": "Inoxmare",        "url": os.environ.get("SUPPLIER_L_URL", "https://www.inoxmare.com/en")},
}


# Keresési URL sablonok — tükrözi a GepcoopPriceAgent _SUPPLIER_SEARCH_URLS szótárát.
# {part_no} helyére a webshop saját cikkszáma kerül (URL-enkódolva).
# None: nincs használható keresési deep-link (pl. SPA vagy autocomplete-alapú)
_SEARCH_URL_TEMPLATES: dict[str, str | None] = {
    "csavarda":  "https://csavarda.hu/pest/kereso?search={part_no}",
    "irontrade": "https://irontrade.hu/kereso?name={part_no}",
    "koelner":   "https://webshop.koelner.hu/termekek/?keres={part_no}",
    "fabory":    "https://www.fabory.com/hu/search?text={part_no}",
    "fastbolt":  "https://fbonline.fastbolt.com/matrix/{part_no}",
    "reyher":    "https://rio.reyher.de/hu/catalogsearch/advanced/result/?sku={part_no}&q=",
    "mekrs":     "https://eshop.mekrs.cz/en/products?nazev={part_no}&onStock=false",
    "wasishop":  "https://www.wasishop.de/de/handel/Artikelliste.php?search={part_no}",
    "hopefix":   None,   # autocomplete-alapú, nincs statikus keresési URL
    "schaefer":  "https://shop.schaefer-peters.com/b2b/en/search/?query={part_no}",
    "kingb2b":   None,   # SPA, nincs termék deep-link
    "inoxmare":  "https://www.inoxmare.com/en/quicksearch/index/resolve/?item={part_no}",
    "argip":     None,
}


def get_supplier_name(supplier_id: str) -> str:
    return SUPPLIERS.get(supplier_id, {}).get("name", supplier_id)


def get_supplier_url(supplier_id: str) -> str:
    return SUPPLIERS.get(supplier_id, {}).get("url", "")


def build_product_link(supplier_id: str, supplier_part_no: str | None, scraped_url: str | None = None) -> str | None:
    """
    Visszaadja a legjobb linket egy termékhez:
    1. Ha a scraper visszaadott konkrét termékoldal URL-t → azt használja
    2. Ha van keresési sablon és supplier_part_no → search URL-t épít
    3. Egyébként None
    """
    if scraped_url:
        return scraped_url
    template = _SEARCH_URL_TEMPLATES.get(supplier_id)
    if template and supplier_part_no:
        return template.replace("{part_no}", quote(str(supplier_part_no), safe=""))
    return None


def list_suppliers() -> list[dict]:
    return [{"id": sid, **meta} for sid, meta in SUPPLIERS.items()]
