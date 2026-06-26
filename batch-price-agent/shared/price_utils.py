import os
import logging
from pathlib import Path
from dotenv import dotenv_values

log = logging.getLogger(__name__)
_DEFAULT_EUR_TO_HUF = 400.0


def _env_file_candidates() -> list[Path]:
    candidates = []
    price_agent_path = os.environ.get("PRICE_AGENT_PATH")
    if price_agent_path:
        candidates.append(Path(price_agent_path) / ".env")
    candidates.append(Path(__file__).resolve().parents[2] / ".env")
    return candidates


def get_eur_to_huf() -> float:
    raw = ""
    for env_file in _env_file_candidates():
        if not env_file.exists():
            continue
        try:
            raw = str(dotenv_values(env_file).get("EUR_TO_HUF_RATE") or "").strip()
        except Exception as exc:
            log.warning("EUR_TO_HUF_RATE nem olvasható a .env fájlból (%s): %s", env_file, exc)
        if raw:
            break
    if not raw:
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
