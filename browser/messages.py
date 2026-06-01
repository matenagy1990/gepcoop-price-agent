"""
Canonical, buyer-facing feedback messages shared by every supplier scraper.

Two distinct outcomes the buyers must be able to tell apart:

- MSG_NOT_FOUND  — the Gép-Coop part is mapped to a supplier part number, but
  after logging in the webshop search returns no matching product.
- MSG_NOT_PRICED — the product IS found in the webshop, but no usable price is
  shown (the price field holds text / a placeholder, or no price at all).

Scrapers raise RuntimeError(MSG_*) at the appropriate point. The base scraping
logic is unchanged; these only standardise the feedback shown on the result card.
"""

MSG_NOT_FOUND = "jelenleg nem találom a webshopban a terméket"
MSG_NOT_PRICED = "a webshopban elérhető a termék, de nincs beárazva"


def has_numeric_price(text: str | None) -> bool:
    """True if `text` contains at least one digit (i.e. a usable price value).

    Used to distinguish a real price from placeholders like 'POA', 'on request',
    'ár kérésre', '—' or an empty cell → those count as 'found but not priced'.
    """
    if not text:
        return False
    return any(ch.isdigit() for ch in text)
