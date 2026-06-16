import os
import logging

log = logging.getLogger(__name__)
_DEFAULT_EUR_TO_HUF = 400.0


def get_eur_to_huf() -> float:
    raw = (os.environ.get("EUR_TO_HUF_RATE", "") or "").strip()
    if not raw:
        return _DEFAULT_EUR_TO_HUF
    try:
        rate = float(raw.replace(",", "."))
        return rate if rate > 0 else _DEFAULT_EUR_TO_HUF
    except ValueError:
        return _DEFAULT_EUR_TO_HUF


def normalize_to_huf_per_db(price_raw: float, price_unit_qty: int, currency: str) -> float | None:
    price_per_db = price_raw / max(price_unit_qty, 1)
    if currency == "HUF":
        return round(price_per_db, 4)
    if currency == "EUR":
        return round(price_per_db * get_eur_to_huf(), 4)
    return None
