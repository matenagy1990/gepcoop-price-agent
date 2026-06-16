import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import pandas as pd
from agent.tools import lookup_mapping_all, fetch_supplier_price, get_all_part_numbers, search_part_numbers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

app = FastAPI(title="Gép-Coop Price Agent", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Max 4 scraper runs simultaneously — prevents Chromium overload under concurrent users
SCRAPER_LIMIT = asyncio.Semaphore(4)

# ── .env helpers ──────────────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / ".env"
DEFAULT_EUR_TO_HUF_RATE = 400.0

def _update_env_file(updates: dict[str, str]) -> None:
    """Update or append key=value pairs in the .env file, then reload os.environ."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True) if ENV_FILE.exists() else []
    updated_keys: set[str] = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")
    ENV_FILE.write_text("".join(new_lines), encoding="utf-8")
    for key, val in updates.items():
        os.environ[key] = val


def _get_manual_eur_huf_rate() -> float:
    raw = (os.environ.get("EUR_TO_HUF_RATE", "") or "").strip()
    if not raw:
        return DEFAULT_EUR_TO_HUF_RATE
    try:
        rate = float(raw.replace(",", "."))
    except ValueError:
        log.warning("Érvénytelen EUR_TO_HUF_RATE érték a környezetben: %r", raw)
        return DEFAULT_EUR_TO_HUF_RATE
    if rate <= 0:
        log.warning("Nem pozitív EUR_TO_HUF_RATE érték a környezetben: %r", raw)
        return DEFAULT_EUR_TO_HUF_RATE
    return rate

# ── Auth / Supabase-backed app users ─────────────────────────────────
PBKDF2_ITERATIONS = 240_000
AUTH_TABLE = "app_users"

_auth_bootstrapped = False


def _normalize_username(username: str) -> str:
    return (username or "").strip()


PRIMARY_ADMIN_USERNAME = _normalize_username(os.environ.get("PRIMARY_ADMIN_USERNAME", "herbstadam@gepcoop.hu"))


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _auth_row_to_user(row: dict) -> dict:
    return {
        "username": row.get("username", ""),
        "is_admin": bool(row.get("is_admin", False)),
        "is_primary": bool(row.get("is_primary", False)),
        "is_active": bool(row.get("is_active", True)),
        "protected": bool(row.get("is_primary", False)),
        "role": "admin" if bool(row.get("is_admin", False)) else "user",
    }


def _get_auth_users(include_inactive: bool = False) -> list[dict]:
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    query = sb.table(AUTH_TABLE).select(
        "username,is_admin,is_primary,is_active,created_at,updated_at,deleted_at"
    )
    if not include_inactive:
        query = query.eq("is_active", True)
    res = query.order("is_primary", desc=True).order("username").execute()
    return res.data or []


def _get_auth_user(username: str, include_inactive: bool = False) -> dict | None:
    username = _normalize_username(username)
    if not username:
        return None
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    query = sb.table(AUTH_TABLE).select("*").eq("username", username).limit(1)
    if not include_inactive:
        query = query.eq("is_active", True)
    res = query.execute()
    return (res.data or [None])[0]


def _upsert_auth_user(username: str, password: str, *, is_admin: bool, is_primary: bool, is_active: bool = True) -> None:
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    username = _normalize_username(username)
    now = datetime.now(timezone.utc).isoformat()
    row = _get_auth_user(username, include_inactive=True)
    payload = {
        "username": username,
        "password_hash": _hash_password(password),
        "is_admin": bool(is_admin),
        "is_primary": bool(is_primary),
        "is_active": bool(is_active),
        "updated_at": now,
        "deleted_at": None if is_active else now,
    }
    if not row:
        payload["created_at"] = now
    sb.table(AUTH_TABLE).upsert(payload, on_conflict="username").execute()


def _set_auth_password(username: str, password: str) -> None:
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    username = _normalize_username(username)
    sb.table(AUTH_TABLE).update({
        "password_hash": _hash_password(password),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "deleted_at": None,
        "is_active": True,
    }).eq("username", username).execute()


def _set_auth_admin(username: str, is_admin: bool) -> None:
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    username = _normalize_username(username)
    sb.table(AUTH_TABLE).update({
        "is_admin": bool(is_admin),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("username", username).execute()


def _soft_delete_auth_user(username: str) -> None:
    sb = _get_supabase_main()
    if sb is None:
        raise RuntimeError("Supabase not configured for app user authentication.")
    username = _normalize_username(username)
    now = datetime.now(timezone.utc).isoformat()
    sb.table(AUTH_TABLE).update({
        "is_active": False,
        "deleted_at": now,
        "updated_at": now,
    }).eq("username", username).execute()


def _ensure_auth_bootstrap() -> None:
    global _auth_bootstrapped
    if _auth_bootstrapped:
        return
    try:
        existing = _get_auth_users(include_inactive=True)
    except Exception as exc:
        raise RuntimeError(
            "Supabase app_users tábla nem elérhető. Hozd létre a deploy/supabase_app_users.sql alapján."
        ) from exc

    if not existing:
        raise RuntimeError(
            "A Supabase app_users tábla üres. Hozz létre legalább egy elsődleges admin felhasználót az admin felületen vagy SQL-ből."
        )
    _auth_bootstrapped = True


def _get_primary_admin_username() -> str:
    _ensure_auth_bootstrap()
    if PRIMARY_ADMIN_USERNAME:
        primary = _get_auth_user(PRIMARY_ADMIN_USERNAME, include_inactive=False)
        if primary and bool(primary.get("is_admin", False)):
            return PRIMARY_ADMIN_USERNAME
    for row in _get_auth_users(include_inactive=False):
        if bool(row.get("is_primary", False)):
            return row.get("username", "")
    admin_rows = [row for row in _get_auth_users(include_inactive=False) if bool(row.get("is_admin", False))]
    if len(admin_rows) == 1:
        return admin_rows[0].get("username", "")
    raise RuntimeError("No primary admin user found in Supabase app_users table.")


def _is_admin_user(username: str) -> bool:
    _ensure_auth_bootstrap()
    row = _get_auth_user(username, include_inactive=False)
    return bool(row and row.get("is_admin", False))


def _is_primary_admin_user(username: str) -> bool:
    _ensure_auth_bootstrap()
    username = _normalize_username(username)
    row = _get_auth_user(username, include_inactive=False)
    if not row:
        return False
    if PRIMARY_ADMIN_USERNAME and username == PRIMARY_ADMIN_USERNAME and bool(row.get("is_admin", False)):
        return True
    if bool(row.get("is_primary", False)):
        return True
    admin_rows = [u for u in _get_auth_users(include_inactive=False) if bool(u.get("is_admin", False))]
    return len(admin_rows) == 1 and admin_rows[0].get("username", "") == username


def _invalidate_user_sessions(username: str) -> None:
    for token, user in list(sessions.items()):
        if user.get("username") == username:
            del sessions[token]


sessions: dict[str, dict[str, object]] = {}   # token → {username,is_admin}

# ── Supplier credentials ──────────────────────────────────────────
SUPPLIER_META = {
    "csavarda":  {"url": "https://csavarda.hu/",                         "env": "SUPPLIER_A", "extra": []},
    "irontrade": {"url": "https://irontrade.hu/",                        "env": "SUPPLIER_B", "extra": []},
    "koelner":   {"url": "https://webshop.koelner.hu/",                  "env": "SUPPLIER_C", "extra": []},
    "mekrs":     {"url": "https://eshop.mekrs.cz/en",                   "env": "SUPPLIER_D", "extra": []},
    "fabory":    {"url": "https://www.fabory.com/hu",                    "env": "SUPPLIER_E", "extra": []},
    "reyher":    {"url": "https://rio.reyher.de",                        "env": "SUPPLIER_F", "extra": [
        {"key": "customer_code", "env_suffix": "CUSTOMER_CODE", "label": "Ügyfélszám"},
    ]},
    "hopefix":   {"url": "https://www.hopefix.cz/en",                   "env": "SUPPLIER_G", "extra": []},
    "fastbolt":  {"url": "https://fbonline.fastbolt.com",               "env": "SUPPLIER_H", "extra": [
        {"key": "shortname", "env_suffix": "SHORTNAME", "label": "Shortname"},
    ]},
    "schaefer":  {"url": "https://shop.schaefer-peters.com/b2b/en/",    "env": "SUPPLIER_I", "extra": []},
    "kingb2b":   {"url": "https://kingb2b.it/PORTAL/",                  "env": "SUPPLIER_J", "extra": []},
    "wasishop":  {"url": "https://www.wasishop.de",                      "env": "SUPPLIER_K", "extra": []},
    "inoxmare":  {"url": "https://www.inoxmare.com/en",                  "env": "SUPPLIER_L", "extra": []},
    "vipa":      {"url": "https://www.vipafasteners.com/en/",            "env": "SUPPLIER_VIPA", "extra": [], "otp": True},
}

QUERY_SUPPLIER_URLS = {
    **{sid: meta["url"] for sid, meta in SUPPLIER_META.items()},
    "argip": "",
}

def _load_supplier_creds_from_env() -> dict:
    result = {}
    for sid, meta in SUPPLIER_META.items():
        env = meta["env"]
        creds = {
            "url":      os.environ.get(f"{env}_URL", meta["url"]),
            "username": os.environ.get(f"{env}_USERNAME", ""),
            "password": os.environ.get(f"{env}_PASSWORD", ""),
        }
        for ex in meta.get("extra", []):
            creds[ex["key"]] = os.environ.get(f"{env}_{ex['env_suffix']}", "")
        result[sid] = creds
    return result

def _apply_suppliers_to_env(suppliers: dict) -> None:
    """Push credentials into os.environ so browser scripts pick them up."""
    for sid, creds in suppliers.items():
        meta = SUPPLIER_META.get(sid, {})
        env  = meta.get("env")
        if env:
            os.environ[f"{env}_URL"]      = creds.get("url", "")
            os.environ[f"{env}_USERNAME"] = creds.get("username", "")
            os.environ[f"{env}_PASSWORD"] = creds.get("password", "")
            for ex in meta.get("extra", []):
                os.environ[f"{env}_{ex['env_suffix']}"] = creds.get(ex["key"], "")

SUPPLIER_CREDS: dict = _load_supplier_creds_from_env()


def _lookup_part_name(part_no: str) -> str:
    """Return the product name (Cikknév) for a given Gép-Coop part number."""
    from agent.tools import _get_supabase
    search = part_no.strip().upper()
    sb = _get_supabase()
    if sb is None:
        return ""
    try:
        res = sb.table("article_mapping").select("name").eq("gepcoop_part_no", search).limit(1).execute()
        if res.data:
            return (res.data[0].get("name") or "").strip()
    except Exception:
        pass
    return ""

UI_FILE        = Path(__file__).parent / "ui" / "index.html"
LOGO_FILE      = Path(__file__).parent / "assets" / "logo.png"
TILE_SINGLE    = Path(__file__).parent / "assets" / "images" / "tile-single.jpg"
TILE_BATCH     = Path(__file__).parent / "assets" / "images" / "tile-batch.jpg"
# Admin-editable homepage info block (shown to every logged-in buyer).
INFO_FILE = Path(__file__).parent / "assets" / "homepage_info.json"   # local mirror
INFO_STORAGE_BUCKET = os.environ.get("SUPABASE_INFO_BUCKET", "internal-docs").strip() or "internal-docs"
INFO_STORAGE_PATH = os.environ.get("SUPABASE_INFO_PATH", "homepage/info.json").strip() or "homepage/info.json"


def _get_username_from_token(token: str | None) -> str:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    if token not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return str(sessions[token]["username"])


def _get_username(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    return _get_username_from_token(token)


def _get_admin(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    session = sessions.get(token)
    if session:
        username = str(session.get("username", ""))
        if username and _is_admin_user(username):
            return username
    raise HTTPException(status_code=401, detail="Invalid or expired admin token")


def _info_storage_bucket(create_if_missing: bool = False):
    sb = _get_supabase_main()
    if sb is None:
        return None
    try:
        if create_if_missing:
            try:
                bucket_names = {
                    (b.get("name") or b.get("id") or "").strip()
                    for b in (sb.storage.list_buckets() or [])
                }
                if INFO_STORAGE_BUCKET not in bucket_names:
                    sb.storage.create_bucket(INFO_STORAGE_BUCKET, options={"public": False})
                    log.info(f"Info storage bucket létrehozva: {INFO_STORAGE_BUCKET}")
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    raise
        return sb.storage.from_(INFO_STORAGE_BUCKET)
    except Exception as exc:
        log.warning(f"Info storage bucket elérése sikertelen: {exc}")
        return None


def _read_homepage_info() -> dict:
    """Return the admin homepage info: {text, updated_at, updated_by}."""
    default = {"text": "", "updated_at": None, "updated_by": None}
    bucket = _info_storage_bucket(create_if_missing=False)
    if bucket is not None:
        try:
            if bucket.exists(INFO_STORAGE_PATH):
                raw = bucket.download(INFO_STORAGE_PATH)
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    return {**default, **data}
        except Exception as exc:
            log.warning(f"Homepage info olvasása Storage-ból sikertelen: {exc}")
    if INFO_FILE.exists():
        try:
            data = json.loads(INFO_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**default, **data}
        except Exception as exc:
            log.warning(f"Helyi homepage info olvasása sikertelen: {exc}")
    return default


def _write_homepage_info(text: str, username: str) -> dict:
    data = {
        "text": text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": username,
    }
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    saved_anywhere = False
    bucket = _info_storage_bucket(create_if_missing=True)
    if bucket is not None:
        try:
            bucket.upload(INFO_STORAGE_PATH, raw, {"content-type": "application/json", "upsert": "true"})
            saved_anywhere = True
        except Exception as exc:
            log.warning(f"Homepage info Storage mentése sikertelen: {exc}")
    try:
        INFO_FILE.parent.mkdir(parents=True, exist_ok=True)
        INFO_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        saved_anywhere = True
    except Exception as exc:
        log.warning(f"Helyi homepage info mentése sikertelen: {exc}")
    if not saved_anywhere:
        raise RuntimeError("A tájékoztató mentése sikertelen (sem Storage, sem helyi fájl).")
    return data


def _hu(n: float, dec: int = 4) -> str:
    """Format a number with Hungarian decimal comma, e.g. 0.2496 → '0,2496'."""
    return f"{n:.{dec}f}".replace(".", ",")


def _hu_int(n: int) -> str:
    """Format an integer with non-breaking space as thousands separator, e.g. 20000 → '20 000'."""
    return f"{n:,}".replace(",", "\u00a0")


def _fmt_stock(stock) -> str:
    """Human-readable stock string for recommendation text (Hungarian)."""
    if stock is None:
        return "ismeretlen"
    if isinstance(stock, str):
        if stock.lower().startswith("raktár"):
            return "raktáron (pontos mennyiség ismeretlen)"
        return stock
    if isinstance(stock, dict):
        v = sum(stock.values())
    else:
        v = int(stock or 0)
    return "nincs készleten" if v == 0 else f"{_hu_int(v)} db"


# ── Recommendation logic ─────────────────────────────────────────────
def compute_recommendation(supplier_results: dict) -> dict:
    """
    Compare supplier results and return a purchase recommendation.
    All suppliers are ranked together. Non-HUF suppliers (currently EUR-based)
    are included using the admin-configured EUR→HUF conversion.
    """
    available = {
        sid: r for sid, r in supplier_results.items()
        if "error" not in r and r.get("price_per_db") is not None
    }

    if not available:
        return {
            "winner": None,
            "reason": "Egyik beszállítótól sem érkezett érvényes áradat.",
        }

    def _rank_price(r: dict) -> float | None:
        """HUF-comparable price used for ranking."""
        if r.get("currency", "HUF") == "HUF":
            return r["price_per_db"]
        return r.get("price_per_db_huf")   # None if FX conversion failed

    def _price_label(sid: str, huf_price: float) -> str:
        """Formatted price string, noting original currency for non-HUF suppliers."""
        r = available[sid]
        curr = r.get("currency", "HUF")
        if curr == "HUF":
            return f"{_hu(huf_price)} HUF/db"
        rate = r.get("fx_huf_rate", "?")
        return (
            f"{_hu(r['price_per_db'])} {curr}/db"
            f" ≈ {_hu(huf_price)} HUF/db (1 {curr} = {_hu(rate, 2)} HUF, admin beállítás)"
        )

    # All suppliers with a usable HUF-comparable price enter the ranking
    rankable = {sid: _rank_price(r) for sid, r in available.items() if _rank_price(r) is not None}

    if not rankable:
        return {
            "winner": None,
            "reason": "Egyik beszállítótól sem érkezett érvényes áradat.",
        }

    if len(rankable) == 1:
        sid = next(iter(rankable))
        r = available[sid]
        return {
            "winner": sid,
            "reason": (
                f"Csak a(z) {sid.capitalize()} adott vissza érvényes árat. "
                f"Ár: {_price_label(sid, rankable[sid])} — "
                f"Készlet: {_fmt_stock(r.get('stock', 0))}."
            ),
            "single_supplier": True,
        }

    sorted_sids = sorted(rankable, key=rankable.get)
    winner      = sorted_sids[0]
    second      = sorted_sids[1]

    winner_price = rankable[winner]
    second_price = rankable[second]
    price_diff   = round(second_price - winner_price, 6)
    savings_pct  = round((price_diff / second_price) * 100, 1) if second_price > 0 else 0.0

    winner_stock_raw = available[winner].get("stock", 0)
    second_stock_raw = available[second].get("stock", 0)
    winner_stock     = _total_stock(winner_stock_raw)
    second_stock     = _total_stock(second_stock_raw)

    stock_note = ""
    if (not isinstance(winner_stock_raw, str)
            and not isinstance(second_stock_raw, str)
            and second_stock > winner_stock * 2):
        stock_note = (
            f" Megjegyzés: a(z) {second.capitalize()} lényegesen nagyobb készlettel rendelkezik "
            f"({_fmt_stock(second_stock_raw)} vs {_fmt_stock(winner_stock_raw)}) — érdemes mérlegelni az elérhetőséget."
        )

    reason = (
        f"Vásárolj a(z) {winner.capitalize()}-tól — "
        f"{_price_label(winner, winner_price)} vs {_price_label(second, second_price)} "
        f"({_hu(savings_pct, 1)}%-kal olcsóbb, darabonként {_hu(price_diff)} HUF megtakarítás)."
        f"{stock_note}"
    )

    all_prices = {sid: round(p, 6) for sid, p in rankable.items()}
    all_stocks = {sid: r.get("stock", 0) for sid, r in available.items()}

    return {
        "winner":      winner,
        "reason":      reason,
        "price_diff":  price_diff,
        "savings_pct": savings_pct,
        "prices":      all_prices,
        "stocks":      all_stocks,
    }


def _total_stock(stock) -> int:
    if isinstance(stock, dict):
        return sum(stock.values())
    if isinstance(stock, str):
        # "Raktáron" = in stock (treat as 1 for comparison purposes)
        return 1 if stock.lower().startswith("raktár") else 0
    return int(stock or 0)



# ── Vipa OTP login state ──────────────────────────────────────────────
# Keeps one active OTP login flow at a time (background asyncio Task).
_vipa_login_state: dict = {
    "active":      False,
    "token_event": None,   # asyncio.Event — set when buyer submits the token
    "ready_event": None,   # asyncio.Event — set when the OTP token field is visible
    "done_event":  None,   # asyncio.Event — set when task finishes
    "token":       None,   # the submitted OTP string
    "error":       None,   # error message from the task, or None on success
    "task":        None,   # the running asyncio.Task
}


async def _run_vipa_otp_login() -> None:
    """Background task: holds a headless Playwright session open for Vipa OTP login."""
    from playwright.async_api import async_playwright as _pw
    from playwright.async_api import TimeoutError as _PwTimeout
    from browser.session_utils import save_session as _save_session
    from browser.supplier_vipa import SESSION_FILE as _VIPA_SESSION, LOGIN_URL as _VIPA_LOGIN, _is_logged_in as _vipa_is_logged_in

    async def _fill_first_input(page, locators: list, value: str, timeout: int = 15000) -> None:
        last_exc = None
        for locator in locators:
            try:
                await locator.first.wait_for(timeout=timeout)
                await locator.first.fill(value)
                return
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("Vipa input mező nem található.")

    async def _click_first_button(page, locators: list, timeout: int = 15000) -> None:
        last_exc = None
        for locator in locators:
            try:
                await locator.first.wait_for(timeout=timeout)
                await locator.first.click()
                return
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("Vipa bejelentkezés gomb nem található.")

    try:
        email = os.environ.get("SUPPLIER_VIPA_USERNAME", "")
        if not email:
            _vipa_login_state["error"] = "SUPPLIER_VIPA_USERNAME nincs beállítva a .env fájlban."
            return

        async with _pw() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(_VIPA_LOGIN, wait_until="domcontentloaded", timeout=30000)
                await _fill_first_input(page, [
                    page.get_by_role("textbox", name=re.compile(r"e-?mail", re.I)),
                    page.locator('input[type="email"]'),
                    page.locator('input[name*="email" i]'),
                    page.locator('input[id*="email" i]'),
                ], email)
                await _click_first_button(page, [
                    page.get_by_role("button", name=re.compile(r"log\\s*in|login|sign\\s*in", re.I)),
                    page.locator('button[type="submit"]'),
                    page.locator('input[type="submit"]'),
                ])

                # Wait for Token input (OTP email sent at this point)
                try:
                    await _fill_first_input(page, [
                        page.get_by_role("textbox", name=re.compile(r"token|otp|code|kód", re.I)),
                        page.locator('input[name*="token" i]'),
                        page.locator('input[id*="token" i]'),
                        page.locator('input[name*="otp" i]'),
                        page.locator('input[id*="otp" i]'),
                    ], "", timeout=15000)
                except _PwTimeout:
                    _vipa_login_state["error"] = "Az OTP e-mail küldése sikertelen – Token mező nem jelent meg."
                    return
                except Exception:
                    _vipa_login_state["error"] = "Az OTP e-mail küldése sikertelen – Token mező nem jelent meg."
                    return

                if _vipa_login_state.get("ready_event"):
                    _vipa_login_state["ready_event"].set()
                log.info("Vipa OTP email sent, waiting for token from admin (max 10 min)")

                # Block here until admin submits the token
                try:
                    await asyncio.wait_for(
                        _vipa_login_state["token_event"].wait(), timeout=600
                    )
                except asyncio.TimeoutError:
                    _vipa_login_state["error"] = "OTP token nem érkezett be 10 percen belül."
                    return

                token = (_vipa_login_state["token"] or "").strip()
                if not token:
                    _vipa_login_state["error"] = "Üres token érkezett."
                    return

                await _fill_first_input(page, [
                    page.get_by_role("textbox", name=re.compile(r"token|otp|code|kód", re.I)),
                    page.locator('input[name*="token" i]'),
                    page.locator('input[id*="token" i]'),
                    page.locator('input[name*="otp" i]'),
                    page.locator('input[id*="otp" i]'),
                ], token, timeout=5000)
                await _click_first_button(page, [
                    page.get_by_role("button", name=re.compile(r"log\\s*in|login|sign\\s*in", re.I)).last,
                    page.locator('button[type="submit"]').last,
                    page.locator('input[type="submit"]').last,
                ], timeout=5000)

                try:
                    await page.wait_for_url("**/customer/**", timeout=15000)
                except _PwTimeout:
                    await page.wait_for_timeout(2000)
                    if not await _vipa_is_logged_in(page):
                        _vipa_login_state["error"] = (
                            "Vipa bejelentkezés sikertelen – érvénytelen vagy lejárt token?"
                        )
                        return

                await _save_session(context, _VIPA_SESSION)
                log.info("Vipa OTP login successful, session saved to %s", _VIPA_SESSION)
                _vipa_login_state["error"] = None

            finally:
                await browser.close()

    except Exception as exc:
        log.exception("Vipa OTP login task failed: %s", exc)
        _vipa_login_state["error"] = str(exc)
    finally:
        _vipa_login_state["active"] = False
        if _vipa_login_state["done_event"]:
            _vipa_login_state["done_event"].set()


async def _vipa_session_is_live() -> bool:
    """Return True only when the saved Vipa session is fresh and accepted by the site."""
    from playwright.async_api import async_playwright as _pw
    from browser.session_utils import invalidate_session, load_session, session_is_fresh
    from browser.supplier_vipa import (
        HOME_URL as _VIPA_HOME,
        SESSION_FILE as _VIPA_SESSION,
        SESSION_MAX_AGE_HOURS as _VIPA_SESSION_MAX_AGE_HOURS,
        _is_logged_in as _vipa_is_logged_in,
    )

    session = load_session(_VIPA_SESSION)
    if not session or not session_is_fresh(session, _VIPA_SESSION_MAX_AGE_HOURS):
        if session:
            invalidate_session(_VIPA_SESSION)
        return False

    try:
        async with _pw() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(storage_state=session["state"])
                page = await context.new_page()
                await page.goto(_VIPA_HOME, wait_until="domcontentloaded", timeout=30000)
                is_live = await _vipa_is_logged_in(page)
                if not is_live:
                    invalidate_session(_VIPA_SESSION)
                return is_live
            finally:
                await browser.close()
    except Exception as exc:
        log.warning("Vipa session live check failed: %s", exc)
        return False


async def _ensure_vipa_session_before_scraping(queue: asyncio.Queue) -> None:
    """
    Block the query pipeline until Vipa has a live session.

    This runs before any supplier scraper starts, so when Vipa is part of the
    query we either continue with a verified session or fail before launching
    the other parallel browser jobs.
    """
    await queue.put(("progress", {
        "step": "browser",
        "status": "running",
        "supplier": "vipa",
        "msg": "Vipa session ellenőrzése…",
    }))
    if await _vipa_session_is_live():
        await queue.put(("progress", {
            "step": "browser",
            "status": "done",
            "supplier": "vipa",
            "msg": "Vipa session aktív, indulhat a lekérdezés.",
        }))
        return

    otp_info = _start_vipa_otp_flow()
    ready_event = _vipa_login_state.get("ready_event")
    if ready_event is None:
        raise RuntimeError("Vipa OTP folyamat nem indult el.")

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=30)
    except asyncio.TimeoutError as exc:
        if _vipa_login_state.get("error"):
            raise RuntimeError(str(_vipa_login_state["error"])) from exc
        raise RuntimeError("Vipa OTP token generálás nem indult el időben.") from exc

    if _vipa_login_state.get("error"):
        raise RuntimeError(str(_vipa_login_state["error"]))

    await queue.put(("password_required", {
        "supplier": "vipa",
        "msg": "Vipa: aktív session nem áll rendelkezésre – OTP bejelentkezés szükséges.",
        "otp": True,
        "otp_email": os.environ.get("SUPPLIER_VIPA_USERNAME", ""),
        "otp_message": otp_info.get("message", ""),
    }))
    await queue.put(("progress", {
        "step": "browser",
        "status": "running",
        "supplier": "vipa",
        "msg": "Vipa OTP tokenre várunk. A többi webshop lekérdezése ezután indul.",
    }))

    done_event = _vipa_login_state.get("done_event")
    if done_event is None:
        raise RuntimeError("Vipa OTP folyamat nem indult el.")

    try:
        await asyncio.wait_for(done_event.wait(), timeout=660)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Vipa OTP token nem érkezett be időben, a lekérdezés nem indult el.") from exc

    if _vipa_login_state.get("error"):
        raise RuntimeError(str(_vipa_login_state["error"]))

    if not await _vipa_session_is_live():
        raise RuntimeError("Vipa bejelentkezés után sem érhető el aktív session.")

    await queue.put(("progress", {
        "step": "browser",
        "status": "done",
        "supplier": "vipa",
        "msg": "Vipa bejelentkezés kész, indulnak a webshop lekérdezések.",
    }))


# ── Models ────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class UpdateUserRequest(BaseModel):
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False

class UpdateAppUserRequest(BaseModel):
    password: str

class UpdateUserRoleRequest(BaseModel):
    is_admin: bool

class SupplierInfoItemRequest(BaseModel):
    label: str = ""
    value: str = ""
    sort_order: int | None = None
    is_active: bool = True

class UpdateSupplierInfoRequest(BaseModel):
    supplier_id: str
    info_items: list[SupplierInfoItemRequest]

class UpdateSupplierRequest(BaseModel):
    supplier_id: str
    username: str
    password: str = ""
    extra: dict | None = None
    info_items: list[SupplierInfoItemRequest] | None = None

class UpdatePasswordRequest(BaseModel):
    supplier_id: str
    password: str

class RunFeedbackRequest(BaseModel):
    run_id: str
    feedback: str


class UpdateFxSettingsRequest(BaseModel):
    eur_to_huf_rate: float


class HomepageInfoRequest(BaseModel):
    text: str = ""


class VipaCompleteLoginRequest(BaseModel):
    token: str


# ── Routes ────────────────────────────────────────────────────────────
@app.get("/")
def serve_ui():
    return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})


@app.get("/logo.png")
def serve_logo():
    return FileResponse(LOGO_FILE, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/tile-single.jpg")
def serve_tile_single():
    return FileResponse(TILE_SINGLE, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/tile-batch.jpg")
def serve_tile_batch():
    return FileResponse(TILE_BATCH, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/homepage-info")
def get_homepage_info(authorization: str | None = Header(default=None)):
    """Return the admin-managed homepage info block (any logged-in user)."""
    _get_username(authorization)
    return _read_homepage_info()


@app.post("/admin/homepage-info")
def set_homepage_info(
    req: HomepageInfoRequest,
    authorization: str | None = Header(default=None),
):
    """Save the homepage info block shown to all buyers (admin only)."""
    admin_username = _get_admin(authorization)
    text = (req.text or "").strip()
    data = _write_homepage_info(text, admin_username)
    log.info(f"Homepage info frissítve: by={admin_username}, length={len(text)}")
    return data


@app.post("/login")
def login(req: LoginRequest):
    _ensure_auth_bootstrap()
    user = _get_auth_user(req.username, include_inactive=False)
    if not user or not _verify_password(req.password, str(user.get("password_hash", ""))):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = secrets.token_hex(32)
    sessions[token] = {
        "username": user["username"],
        "is_admin": bool(user.get("is_admin", False)),
    }
    return {
        "token": token,
        "username": user["username"],
        "is_admin": bool(user.get("is_admin", False)),
        "is_primary_admin": _is_primary_admin_user(user["username"]),
    }


@app.get("/me")
def get_me(
    authorization: str | None = Header(default=None),
):
    username = _get_username(authorization)
    return {
        "username": username,
        "is_admin": _is_admin_user(username),
        "is_primary_admin": _is_primary_admin_user(username),
    }


@app.get("/query/lookup")
async def query_lookup(
    internal_part_no: str,
    authorization: str | None = Header(default=None),
):
    """
    Step 1: Look up a Gép-Coop part number in the mapping table.
    Returns the product name and all supplier part numbers — without scraping anything.
    The frontend shows this to the user for confirmation before starting the actual search.
    """
    _get_username(authorization)
    part = internal_part_no.strip()
    suppliers = lookup_mapping_all(part)
    name = _lookup_part_name(part)
    info_by_supplier = _supplier_info_items_for([s["supplier_id"] for s in suppliers])
    for supplier in suppliers:
        supplier["info_items"] = info_by_supplier.get(supplier["supplier_id"], [])

    found_ids = {s["supplier_id"] for s in suppliers}
    unavailable = [
        {"supplier_id": sid, "supplier_url": url}
        for sid, url in QUERY_SUPPLIER_URLS.items()
        if sid not in found_ids
    ]

    return {"part_no": part, "name": name, "suppliers": suppliers, "unavailable": unavailable}


_sb_main = None

def _get_supabase_main():
    global _sb_main
    if _sb_main is not None:
        return _sb_main
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _sb_main = create_client(url, key)
    except Exception as exc:
        log.warning(f"Supabase (main) init failed: {exc}")
        _sb_main = None
    return _sb_main


def _supplier_info_items_for(supplier_ids: list[str]) -> dict[str, list[dict]]:
    """Return active ordering info rows grouped by supplier id."""
    clean_ids = sorted({(sid or "").strip().lower() for sid in supplier_ids if (sid or "").strip()})
    if not clean_ids:
        return {}
    sb = _get_supabase_main()
    if sb is None:
        return {sid: [] for sid in clean_ids}
    try:
        res = (
            sb.table("supplier_info_items")
            .select("supplier_id,label,value,sort_order,is_active,updated_at")
            .in_("supplier_id", clean_ids)
            .eq("is_active", True)
            .order("sort_order", desc=False)
            .execute()
        )
    except Exception as exc:
        log.warning(f"supplier_info_items lekérés sikertelen: {exc}")
        return {sid: [] for sid in clean_ids}

    grouped = {sid: [] for sid in clean_ids}
    for row in res.data or []:
        sid = (row.get("supplier_id") or "").strip().lower()
        label = (row.get("label") or "").strip()
        value = (row.get("value") or "").strip()
        if not sid or not label or not value:
            continue
        grouped.setdefault(sid, []).append({
            "label": label,
            "value": value,
            "sort_order": row.get("sort_order"),
            "is_active": bool(row.get("is_active", True)),
            "updated_at": row.get("updated_at"),
        })
    return {sid: items[:5] for sid, items in grouped.items()}


def _sanitize_supplier_info_items(items: list[SupplierInfoItemRequest] | None) -> list[dict]:
    if not items:
        return []
    cleaned = []
    for idx, item in enumerate(items):
        label = (item.label or "").strip()
        value = (item.value or "").strip()
        if not label or not value or not bool(item.is_active):
            continue
        cleaned.append({
            "label": label[:80],
            "value": value[:500],
            "sort_order": item.sort_order if item.sort_order is not None else idx + 1,
            "is_active": True,
        })
    if len(cleaned) > 5:
        raise HTTPException(status_code=400, detail="Webshoponként legfeljebb 5 aktív információs sor menthető.")
    for idx, item in enumerate(cleaned, start=1):
        item["sort_order"] = idx
    return cleaned


def _replace_supplier_info_items(supplier_id: str, items: list[SupplierInfoItemRequest] | None, updated_by: str) -> list[dict]:
    supplier_id = (supplier_id or "").strip().lower()
    cleaned = _sanitize_supplier_info_items(items)
    sb = _get_supabase_main()
    if sb is None:
        if cleaned:
            raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva, az információs sorok nem menthetők.")
        return []
    try:
        sb.table("supplier_info_items").delete().eq("supplier_id", supplier_id).execute()
        if cleaned:
            now = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    "supplier_id": supplier_id,
                    "label": item["label"],
                    "value": item["value"],
                    "sort_order": item["sort_order"],
                    "is_active": True,
                    "updated_at": now,
                    "updated_by": updated_by,
                }
                for item in cleaned
            ]
            sb.table("supplier_info_items").insert(rows).execute()
    except Exception as exc:
        log.warning(f"supplier_info_items mentés sikertelen: {exc}")
        raise HTTPException(
            status_code=503,
            detail="A rendelési információk mentése sikertelen. Futtasd a deploy/supabase_supplier_info_items.sql migrációt.",
        )
    return _supplier_info_items_for([supplier_id]).get(supplier_id, [])


def _save_run(run_id, part_no, started_at, status,
              suppliers_queried, suppliers_ok, suppliers_error,
              error_message, duration_ms, username=None):
    sb = _get_supabase_main()
    if sb is None:
        return
    payload = {
        "run_id":           run_id,
        "gepcoop_part_no":  part_no,
        "started_at":       started_at.isoformat(),
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "status":           status,
        "suppliers_queried": suppliers_queried,
        "suppliers_ok":     suppliers_ok,
        "suppliers_error":  suppliers_error,
        "error_message":    error_message,
        "duration_ms":      duration_ms,
    }
    if username:
        payload["username"] = username
    try:
        sb.table("query_runs").upsert(payload, on_conflict="run_id").execute()
    except Exception as exc:
        if username and "username" in str(exc).lower():
            payload.pop("username", None)
            try:
                sb.table("query_runs").upsert(payload, on_conflict="run_id").execute()
                log.warning("query_runs.username oszlop hiányzik; futás user nélkül mentve. Futtasd a deploy/supabase_query_runs_username.sql migrációt.")
                return
            except Exception as retry_exc:
                exc = retry_exc
        log.warning(f"run log mentés sikertelen: {exc}")


def _is_missing_feedback_columns(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "recommendation_feedback" in msg
        or "recommendation_feedback_at" in msg
        or "recommendation_feedback_by" in msg
    )


def _require_feedback_columns(exc: Exception):
    if _is_missing_feedback_columns(exc):
        raise HTTPException(
            status_code=503,
            detail="A feedback oszlopok hiányoznak. Futtasd a deploy/supabase_query_runs_feedback.sql migrációt.",
        )
    raise HTTPException(status_code=500, detail=f"Feedback mentés sikertelen: {exc}")


@app.get("/query/stream")
async def query_stream(
    internal_part_no: str,
    suppliers: str | None = None,
    authorization: str | None = Header(default=None),
):
    run_username = _get_username(authorization)

    queue: asyncio.Queue = asyncio.Queue()

    async def runner():
        part       = internal_part_no.strip()
        run_id     = secrets.token_hex(8)
        started_at = datetime.now(timezone.utc)

        # run-level tracking state
        _suppliers_queried: list[str] = []
        _suppliers_ok:      list[str] = []
        _suppliers_error:   list[str] = []
        _error_message:     str | None = None
        _run_status:        str = "error"

        try:
            # ── Step 1: mapping lookup ───────────────────────────────
            await queue.put(("progress", {
                "step": "mapping", "status": "running",
                "msg": f"Looking up '{part}' in mapping table…",
            }))

            supplier_list = lookup_mapping_all(part)
            log.info(f"[{run_id}] Mapping result for '{part}': {supplier_list}")

            if not supplier_list:
                msg = f"Part number '{part}' was not found in the supplier mapping."
                _error_message = msg
                await queue.put(("progress", {"step": "mapping", "status": "error", "msg": msg}))
                await queue.put(("error", {"message": msg}))
                return

            # ── Optional: filter to selected suppliers ──
            if suppliers:
                filter_ids = {s.strip() for s in suppliers.split(",") if s.strip()}
                supplier_list = [s for s in supplier_list if s["supplier_id"] in filter_ids]
                if not supplier_list:
                    msg = f"A '{part}' cikkszám nem elérhető a kiválasztott beszállítóknál."
                    _error_message = msg
                    await queue.put(("progress", {"step": "mapping", "status": "error", "msg": msg}))
                    await queue.put(("error", {"message": msg}))
                    return

            _suppliers_queried = [s["supplier_id"] for s in supplier_list]
            supplier_labels = ", ".join(_suppliers_queried)
            await queue.put(("progress", {
                "step": "mapping", "status": "done",
                "msg": f"Found {len(supplier_list)} supplier(s): {supplier_labels}",
            }))

            if any(s["supplier_id"] == "vipa" for s in supplier_list):
                await _ensure_vipa_session_before_scraping(queue)

            # ── Step 2: parallel fetch for all suppliers ─────────────
            results: dict = {}

            async def fetch_one(sup: dict):
                sid = sup["supplier_id"]

                async def on_progress(ev: dict):
                    ev["supplier"] = sid
                    await queue.put(("progress", ev))

                async with SCRAPER_LIMIT:
                    try:
                        r = await fetch_supplier_price(
                            sid, sup["supplier_part_no"], on_progress=on_progress
                        )
                        results[sid] = r
                        log.info(f"[{run_id}][{sid}] fetch done: price_per_db={r.get('price_per_db')}")
                    except Exception as exc:
                        log.error(f"[{run_id}][{sid}] fetch failed: {exc}")
                        err_msg = str(exc)
                        results[sid] = {"error": err_msg, "supplier_id": sid}
                        # Vipa OTP required — auto-start the OTP email flow and
                        # ask the buyer for the token on the main page.
                        if sid == "vipa" and "aktív session nem áll rendelkezésre" in err_msg.lower():
                            otp_info = _start_vipa_otp_flow()
                            await queue.put(("password_required", {
                                "supplier": sid,
                                "msg": err_msg,
                                "otp": True,
                                "otp_email": os.environ.get("SUPPLIER_VIPA_USERNAME", ""),
                                "otp_message": otp_info.get("message", ""),
                            }))
                        else:
                            # Generic login failures → prompt user to update password
                            _login_fail_keywords = (
                                "please check credentials",
                                "check credentials",
                                "authentication failed",
                                "unauthorized",
                                "invalid user",
                                "invalid password",
                                "wrong password",
                                "hibás jelszó",
                                "érvénytelen felhasználó",
                            )
                            _is_login_fail = any(kw in err_msg.lower() for kw in _login_fail_keywords)
                            if _is_login_fail:
                                await queue.put(("password_required", {
                                    "supplier": sid,
                                    "msg": err_msg,
                                }))
                            else:
                                await queue.put(("progress", {
                                    "step": "browser", "status": "error",
                                    "msg": err_msg, "supplier": sid,
                                }))

            await asyncio.gather(*[fetch_one(s) for s in supplier_list])

            # ── Collect ok/error per supplier ──────────────────────
            info_by_supplier = _supplier_info_items_for(_suppliers_queried)
            for sid, result in results.items():
                result["info_items"] = info_by_supplier.get(sid, [])

            for sid, r in results.items():
                if "error" in r:
                    _suppliers_error.append(sid)
                else:
                    _suppliers_ok.append(sid)

            if _suppliers_ok and _suppliers_error:
                _run_status = "partial"
            elif _suppliers_ok:
                _run_status = "ok"
            else:
                _run_status = "error"
                _error_message = "; ".join(
                    results[s].get("error", "") for s in _suppliers_error
                )

            # ── Step 3: recommendation ───────────────────────────────
            recommendation = compute_recommendation(results)
            log.info(f"[{run_id}] Recommendation: {recommendation}")

            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            await asyncio.to_thread(
                _save_run, run_id, part, started_at, _run_status,
                _suppliers_queried, _suppliers_ok, _suppliers_error,
                _error_message, duration_ms, run_username,
            )

            await queue.put(("result", {
                "run_id":           run_id,
                "internal_part_no": part,
                "suppliers":        results,
                "recommendation":   recommendation,
            }))

        except Exception as exc:
            log.exception(f"[{run_id}] Unexpected error in runner: {exc}")
            _error_message = str(exc)
            await queue.put(("error", {"message": str(exc)}))
        finally:
            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            await asyncio.to_thread(
                _save_run, run_id, part, started_at, _run_status,
                _suppliers_queried, _suppliers_ok, _suppliers_error,
                _error_message, duration_ms, run_username,
            )
            await queue.put(None)

    asyncio.create_task(runner())

    async def generate():
        while True:
            item = await queue.get()
            if item is None:
                break
            evt_type, data = item
            payload = json.dumps(data, ensure_ascii=False)
            yield f"event: {evt_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/query/feedback")
def save_query_feedback(
    req: RunFeedbackRequest,
    authorization: str | None = Header(default=None),
):
    username = _get_username(authorization)
    run_id = (req.run_id or "").strip()
    feedback = (req.feedback or "").strip().lower()
    if not run_id:
        raise HTTPException(status_code=400, detail="Hiányzó run_id.")
    if feedback not in {"positive", "negative"}:
        raise HTTPException(status_code=400, detail="Érvénytelen feedback érték.")

    sb = _get_supabase_main()
    if sb is None:
        raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva.")

    try:
        existing = (
            sb.table("query_runs")
            .select("run_id,recommendation_feedback")
            .eq("run_id", run_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        _require_feedback_columns(exc)

    row = (existing.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="A futás nem található.")
    if row.get("recommendation_feedback"):
        return {"saved": False, "already_set": True}

    payload = {
        "recommendation_feedback": feedback,
        "recommendation_feedback_at": datetime.now(timezone.utc).isoformat(),
        "recommendation_feedback_by": username,
    }
    try:
        sb.table("query_runs").update(payload).eq("run_id", run_id).execute()
    except Exception as exc:
        _require_feedback_columns(exc)

    return {"saved": True}


@app.get("/parts")
def get_parts(authorization: str | None = Header(default=None)):
    _get_username(authorization)
    return {"parts": get_all_part_numbers()}


@app.get("/parts/search")
def search_parts(q: str = "", authorization: str | None = Header(default=None)):
    _get_username(authorization)
    if not q or len(q) < 2:
        return {"parts": []}
    return {"parts": search_part_numbers(q, limit=20)}


# Product/search deep-link templates used by /supplier/open.
# These open in the buyer's OWN browser, so they work both locally and when the
# app is hosted (e.g. on Hetzner). When the buyer is logged in to the webshop in
# that browser, the page shows the same authenticated prices the scraper found.
# kingb2b is a pure SPA with no product deep-link, so it can only open the portal
# home — the buyer searches the part number there manually.
_SUPPLIER_SEARCH_URLS: dict[str, str] = {
    "csavarda":  "https://csavarda.hu/pest/kereso?search={part_no}",
    "irontrade": "https://irontrade.hu/kereso?name={part_no}",
    "koelner":   "https://webshop.koelner.hu/termekek/?keres={part_no}",
    "fabory":    "https://www.fabory.com/hu/search?text={part_no}",
    "fastbolt":  "https://fbonline.fastbolt.com/matrix/{part_no}",
    "reyher":    "https://rio.reyher.de/hu/catalogsearch/advanced/result/?sku={part_no}&q=",
    "mekrs":     "https://eshop.mekrs.cz/en/products?nazev={part_no}&onStock=false",
    "wasishop":  "https://www.wasishop.de/de/handel/Artikelliste.php?search={part_no}",
    "hopefix":   "https://www.hopefix.cz/en",
    "schaefer":  "https://shop.schaefer-peters.com/b2b/en/search/?query={part_no}",
    "kingb2b":   "https://kingb2b.it/PORTAL/",
    "inoxmare":  "https://www.inoxmare.com/en/quicksearch/index/resolve/?item={part_no}",
    "vipa":      "https://www.vipafasteners.com/en/search/?q={part_no}",
}


# Login-page URLs per supplier, used by the "log in to webshops" helper.
# Values mirror each scraper's LOGIN_URL constant (kingb2b logs in on its portal).
_SUPPLIER_LOGIN_URLS: dict[str, str] = {
    "csavarda":  "https://csavarda.hu/bejelentkezes",
    "irontrade": "https://irontrade.hu/bejelentkezes",
    "koelner":   "https://webshop.koelner.hu/belepes/",
    "mekrs":     "https://eshop.mekrs.cz/en",
    "fabory":    "https://www.fabory.com/hu/login",
    "reyher":    "https://rio.reyher.de/hu/customer/account/login",
    "hopefix":   "https://www.hopefix.cz/en/login",
    "fastbolt":  "https://fbonline.fastbolt.com/login",
    "schaefer":  "https://shop.schaefer-peters.com/sp/en/login/",
    "kingb2b":   "https://kingb2b.it/PORTAL/",
    "wasishop":  "https://www.wasishop.de/login_form.php",
    "inoxmare":  "https://www.inoxmare.com/en/customer/account/login/",
    "vipa":      "https://www.vipafasteners.com/en/login/",
}


def _build_supplier_url(sid: str, supplier_part_no: str = "") -> str:
    """Return the best URL for a supplier — search page if part_no available, else home."""
    from urllib.parse import quote
    template = _SUPPLIER_SEARCH_URLS.get(sid, SUPPLIER_META.get(sid, {}).get("url", ""))
    if supplier_part_no and "{part_no}" in template:
        return template.replace("{part_no}", quote(supplier_part_no, safe=""))
    # No part_no or no placeholder → strip template vars, fall back to home
    if "{part_no}" in template:
        return SUPPLIER_META.get(sid, {}).get("url", template.split("{")[0])
    return template


@app.post("/supplier/open")
async def supplier_open(req: Request, authorization: str | None = Header(default=None)):
    """
    Open a supplier's product page for the buyer.

    Returns a deep-link URL that the frontend opens in the buyer's OWN browser
    (a new tab). This works both locally and when the app is hosted, because the
    server only hands over a URL — it does not try to open a window itself. When
    the buyer is logged in to the webshop in that browser, the page shows the
    same authenticated prices the scraper found.

    `is_product_link` tells the frontend whether the URL points at the specific
    part (true) or only the supplier home/portal (false, e.g. kingb2b's SPA),
    so it can hint the buyer to search the part number manually.
    """
    _get_username(authorization)
    data = await req.json()
    sid              = (data.get("supplier_id") or "").strip().lower()
    supplier_part_no = (data.get("supplier_part_no") or "").strip()

    if sid not in SUPPLIER_META:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {sid}")

    url = _build_supplier_url(sid, supplier_part_no)
    template = _SUPPLIER_SEARCH_URLS.get(sid, "")
    is_product_link = bool(supplier_part_no) and "{part_no}" in template
    return {
        "status": "redirect",
        "url": url,
        "is_product_link": is_product_link,
        "supplier_part_no": supplier_part_no,
    }


@app.get("/supplier/login-info")
def supplier_login_info(authorization: str | None = Header(default=None)):
    """
    Return the login page + shared company credentials for every supplier, so the
    buyer can log in to each webshop once in their own browser. After that the
    browser remembers the session and the "Tovább a honlapra" deep-links land on
    the authenticated product page.

    Requires a logged-in app user (not admin). The credentials are the shared
    company webshop accounts the buyers already use.
    """
    _get_username(authorization)
    result = []
    for sid, meta in SUPPLIER_META.items():
        creds = SUPPLIER_CREDS.get(sid, {})
        result.append({
            "id":        sid,
            "login_url": _SUPPLIER_LOGIN_URLS.get(sid, meta.get("url", "")),
            "username":  creds.get("username", ""),
            "password":  creds.get("password", ""),
            "extra": [
                {"label": ex["label"], "value": creds.get(ex["key"], "")}
                for ex in meta.get("extra", [])
            ],
        })
    return {"suppliers": result}


async def _supplier_open_headed(sid: str, supplier_part_no: str) -> None:
    """Launch a headed (visible) browser, restoring saved session if available."""
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    from pathlib import Path as _Path
    from urllib.parse import quote as _quote
    from browser.session_utils import invalidate_session as _invalidate_session
    from browser.session_utils import load_session as _load_session
    from browser.session_utils import save_session as _save_session

    _SESSIONS_DIR = _Path(__file__).parent / "assets" / "sessions"

    SESSION_FILES = {
        "koelner":  _SESSIONS_DIR / "koelner_session.json",
        "reyher":   _SESSIONS_DIR / "reyher_session.json",
        "csavarda": _SESSIONS_DIR / "csavarda_session.json",
        "irontrade":_SESSIONS_DIR / "irontrade_session.json",
        "fabory":   _SESSIONS_DIR / "fabory_session.json",
        "hopefix":  _SESSIONS_DIR / "hopefix_session.json",
        "wasishop": _SESSIONS_DIR / "wasishop_session.json",
        "mekrs":    _SESSIONS_DIR / "mekrs_session.json",
        "fastbolt": _SESSIONS_DIR / "fastbolt_session.json",
        "schaefer": _SESSIONS_DIR / "schaefer_session.json",
        "kingb2b":  _SESSIONS_DIR / "kingb2b_session.json",
    }

    # Search URLs for storage_state suppliers (opened directly after session restore)
    _STORAGE_STATE_SEARCH_URLS = {
        "csavarda":  "https://csavarda.hu/pest/kereso?search={part_no}",
        "irontrade": "https://irontrade.hu/kereso?name={part_no}",
        "fabory":    "https://www.fabory.com/hu/search?text={part_no}",
        "mekrs":     "https://eshop.mekrs.cz/en/products?nazev={part_no}&onStock=false",
        "hopefix":   "https://www.hopefix.cz/en",
        "wasishop":  "https://www.wasishop.de",
        "fastbolt":  "https://fbonline.fastbolt.com/matrix/{part_no}",
        "schaefer":  "https://shop.schaefer-peters.com/b2b/en/search/?query={part_no}",
        "kingb2b":   "https://kingb2b.it/PORTAL/",
    }
    STORAGE_HOME_URLS = {
        "csavarda":  "https://csavarda.hu/",
        "irontrade": "https://irontrade.hu/",
        "fabory":    "https://www.fabory.com/hu",
        "mekrs":     "https://eshop.mekrs.cz/en",
        "hopefix":   "https://www.hopefix.cz/en",
        "wasishop":  "https://www.wasishop.de",
        "fastbolt":  "https://fbonline.fastbolt.com",
        "schaefer":  "https://shop.schaefer-peters.com/b2b/en/",
        "kingb2b":   "https://kingb2b.it/PORTAL/",
    }
    LOGIN_URLS = {
        "koelner": "https://webshop.koelner.hu/belepes/",
        "reyher":  "https://rio.reyher.de/hu/customer/account/login",
    }
    HOME_URLS = {
        "koelner": "https://webshop.koelner.hu/",
        "reyher":  "https://rio.reyher.de/hu/",
    }
    SEARCH_URLS = {
        "koelner": "https://webshop.koelner.hu/termekek/?keres={part_no}",
        "reyher":  "https://rio.reyher.de/hu/catalogsearch/advanced/result/?sku={part_no}&q=",
    }

    session_file = SESSION_FILES[sid]

    # ── Generic storage_state suppliers (csavarda, irontrade, fabory, mekrs, hopefix, wasishop)
    if sid in _STORAGE_STATE_SEARCH_URLS:
        url_tpl = _STORAGE_STATE_SEARCH_URLS[sid]
        if supplier_part_no and "{part_no}" in url_tpl:
            target_url = url_tpl.replace("{part_no}", _quote(supplier_part_no, safe=""))
        elif "{part_no}" in url_tpl:
            target_url = STORAGE_HOME_URLS.get(sid, url_tpl.split("{")[0])
        else:
            target_url = url_tpl
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False)
                context = None
                session = _load_session(session_file)
                if session:
                    try:
                        context = await browser.new_context(storage_state=session["state"])
                        log.info(f"[{sid}/open] Session restored from {session_file}")
                    except Exception as exc:
                        log.warning(f"[{sid}/open] Session unreadable: {exc}")
                if context is None:
                    context = await browser.new_context()
                page = await context.new_page()
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                log.info(f"[{sid}/open] Navigated to {target_url}")
                if sid == "fabory":
                    try:
                        await page.get_by_role("button", name="Összes elfogadása").click(timeout=5000)
                        log.info("[fabory/open] Cookie banner accepted")
                    except PlaywrightTimeout:
                        log.info("[fabory/open] No cookie banner appeared")
                await page.wait_for_event("close", timeout=0)
        except Exception as exc:
            log.warning(f"[{sid}/open] Browser closed or error: {exc}")
        return

    target_url   = (
        SEARCH_URLS[sid].format(part_no=supplier_part_no)
        if supplier_part_no else HOME_URLS[sid]
    )

    try:
        async with async_playwright() as pw:
            # ── koelner uses storage_state (localStorage + cookies) ──────────
            if sid == "koelner":
                context = None
                session = _load_session(session_file)
                if session:
                    try:
                        browser  = await pw.chromium.launch(headless=False)
                        context  = await browser.new_context(storage_state=session["state"])
                        log.info(f"[{sid}/open] Session restored from {session_file}")
                    except Exception as exc:
                        log.warning(f"[{sid}/open] Session unreadable, fresh login: {exc}")
                        _invalidate_session(session_file)
                if context is None:
                    browser = await pw.chromium.launch(headless=False)
                    context = await browser.new_context()
                page = await context.new_page()
                # Verify session
                await page.goto(LOGIN_URLS[sid], wait_until="domcontentloaded", timeout=15000)
                if await page.locator("#login_username").count() > 0:
                    # Session expired — full login
                    log.info(f"[{sid}/open] Session expired — logging in")
                    await page.locator("#login_username").fill(os.environ.get("SUPPLIER_C_USERNAME", ""))
                    await page.locator("#login_password").fill(os.environ.get("SUPPLIER_C_PASSWORD", ""))
                    await page.locator("#loginbutton").click()
                    try:
                        await page.locator("#login_username").wait_for(state="hidden", timeout=10000)
                        log.info(f"[{sid}/open] Login successful")
                        await _save_session(context, session_file)
                    except PlaywrightTimeout:
                        log.warning(f"[{sid}/open] Login may have failed — continuing anyway")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)

            # ── reyher uses shared storage_state session format ──────────────
            else:
                browser = await pw.chromium.launch(headless=False)
                context = await browser.new_context()
                page    = await context.new_page()
                session_restored = False
                session = _load_session(session_file)
                if session:
                    try:
                        await context.close()
                        context = await browser.new_context(storage_state=session["state"])
                        page = await context.new_page()
                        session_restored = True
                        log.info(f"[{sid}/open] Session restored from {session_file}")
                    except Exception as exc:
                        log.warning(f"[{sid}/open] Could not restore session: {exc}")

                if session_restored:
                    await page.goto(HOME_URLS[sid], wait_until="domcontentloaded", timeout=15000)
                    try:
                        await page.wait_for_selector("a:has-text('Quickinput')", timeout=5000)
                        log.info(f"[{sid}/open] Session valid")
                    except PlaywrightTimeout:
                        session_restored = False
                        log.info(f"[{sid}/open] Session expired — logging in")

                if not session_restored:
                    await page.goto(LOGIN_URLS[sid], wait_until="domcontentloaded", timeout=15000)
                    for btn in ("Allow all", "Mindent engedélyez", "Accept all"):
                        try:
                            await page.get_by_role("button", name=btn).click(timeout=3000)
                            break
                        except PlaywrightTimeout:
                            continue
                    if "/customer/account/login" in page.url:
                        await page.locator("#customernumber").fill(os.environ.get("SUPPLIER_F_CUSTOMER_CODE", ""))
                        await page.locator("#userid").fill(os.environ.get("SUPPLIER_F_USERNAME", ""))
                        await page.locator("#pass").fill(os.environ.get("SUPPLIER_F_PASSWORD", ""))
                        await page.get_by_role("button", name="Bejelentkezés").click()
                        try:
                            await page.wait_for_url(HOME_URLS[sid], timeout=15000)
                            await page.wait_for_load_state("networkidle", timeout=20000)
                            await _save_session(context, session_file)
                        except PlaywrightTimeout:
                            log.warning(f"[{sid}/open] Login timeout — continuing")

                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                log.info(f"[{sid}/open] Navigated to {target_url}")

            await page.wait_for_event("close", timeout=0)

    except Exception as exc:
        log.warning(f"[{sid}/open] Browser closed or error: {exc}")


@app.post("/reyher/open")
async def reyher_open(req: Request, authorization: str | None = Header(default=None)):
    """
    Launch a headed (visible) Playwright browser, log in to rio.reyher.de,
    and navigate to the search results for the given supplier_part_no.
    The browser stays open for the buyer to use manually.
    Returns immediately — the browser runs in the background.
    """
    _get_username(authorization)
    data = await req.json()
    supplier_part_no = (data.get("supplier_part_no") or "").strip()
    if not supplier_part_no:
        raise HTTPException(status_code=400, detail="supplier_part_no kötelező.")
    asyncio.create_task(_reyher_open_headed(supplier_part_no))
    return {"status": "opening"}


async def _reyher_open_headed(supplier_part_no: str) -> None:
    """Open a headed Chromium window logged in to Reyher, searching for supplier_part_no."""
    await _supplier_open_headed("reyher", supplier_part_no)


def _start_vipa_otp_flow() -> dict:
    """
    Kick off the headless OTP login flow if one is not already running.
    Navigates to the Vipa login page, enters the configured email and clicks
    'Log in' — this triggers the OTP email. The background task then waits for
    the token via POST /vipa/complete-login.

    Returns a dict with ok/message describing the result. Safe to call when a
    flow is already active (it will report that one is in progress).
    """
    email = os.environ.get("SUPPLIER_VIPA_USERNAME", "—")
    if _vipa_login_state["active"]:
        return {
            "ok": True,
            "already_running": True,
            "message": f"OTP folyamat már fut. Adja meg a(z) {email} címre érkezett tokent.",
        }

    _vipa_login_state["active"]      = True
    _vipa_login_state["token"]       = None
    _vipa_login_state["error"]       = None
    _vipa_login_state["token_event"] = asyncio.Event()
    _vipa_login_state["ready_event"] = asyncio.Event()
    _vipa_login_state["done_event"]  = asyncio.Event()
    _vipa_login_state["task"]        = asyncio.create_task(_run_vipa_otp_login())

    return {
        "ok": True,
        "already_running": False,
        "message": f"OTP e-mail elküldve a(z) {email} címre. Ellenőrizze a postaládát, majd írja be a tokent.",
    }


@app.post("/vipa/initiate-login")
async def vipa_initiate_login(authorization: str | None = Header(default=None)):
    """
    Start a headless OTP login flow for Vipa. Available to any authenticated
    user so a buyer can request the token straight from the main page.
    """
    _get_username(authorization)
    return _start_vipa_otp_flow()


@app.post("/vipa/complete-login")
async def vipa_complete_login(
    req: VipaCompleteLoginRequest,
    authorization: str | None = Header(default=None),
):
    """
    Submit the OTP token received by email to complete the Vipa login.
    Available to any authenticated user. Waits up to 30 s for the background
    task to finish, then returns the result.
    """
    _get_username(authorization)
    if not _vipa_login_state["active"]:
        raise HTTPException(
            status_code=400,
            detail="Nincs aktív Vipa OTP folyamat. Először kattintson az 'OTP kérése' gombra.",
        )
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="A token nem lehet üres.")

    _vipa_login_state["token"] = token
    _vipa_login_state["token_event"].set()

    try:
        await asyncio.wait_for(_vipa_login_state["done_event"].wait(), timeout=30)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "A bejelentkezési folyamat időtúllépés miatt nem fejeződött be."}

    if _vipa_login_state["error"]:
        raise HTTPException(status_code=400, detail=_vipa_login_state["error"])

    return {"ok": True, "message": "Vipa bejelentkezés sikeres! A session el lett mentve."}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/supplier/update-password")
def update_supplier_password(
    req: UpdatePasswordRequest,
    authorization: str | None = Header(default=None),
):
    """Allow an admin to update a supplier's password (e.g. after Schaefer monthly rotation)."""
    _get_admin(authorization)
    if req.supplier_id not in SUPPLIER_CREDS:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {req.supplier_id}")
    if not req.password:
        raise HTTPException(status_code=400, detail="Jelszó megadása kötelező.")
    env_prefix = SUPPLIER_META[req.supplier_id]["env"]
    SUPPLIER_CREDS[req.supplier_id]["password"] = req.password
    _update_env_file({f"{env_prefix}_PASSWORD": req.password})
    _apply_suppliers_to_env(SUPPLIER_CREDS)
    log.info(f"Jelszó frissítve: {req.supplier_id}")
    return {"ok": True}


# ── Admin routes ──────────────────────────────────────────────────────

@app.get("/admin/mapping")
def admin_get_mapping(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    from agent.tools import _get_supabase
    sb = _get_supabase()
    if sb is None:
        return {"columns": [], "rows": []}
    try:
        res = sb.table("article_mapping").select("*").limit(10).execute()
        rows = res.data or []
        columns = list(rows[0].keys()) if rows else []
    except Exception:
        columns, rows = [], []
    return {"columns": columns, "rows": rows}


_MAPPING_COL_ALIASES: dict[str, str] = {
    "cikkszám":         "gepcoop_part_no",
    "cikkszam":         "gepcoop_part_no",
    "gépcoop cikkszám": "gepcoop_part_no",
    "gepcoop cikkszám": "gepcoop_part_no",
    "gepcoop cikkszam": "gepcoop_part_no",
    "cikknév":          "name",
    "cikknev":          "name",
    "csavarda":         "csavarda_part_no",
    "iron trade":       "irontrade_part_no",
    "irontrade":        "irontrade_part_no",
    "koelner":          "koelner_part_no",
    "mekrs":            "mekrs_part_no",
    "fabory":           "fabory_part_no",
    "ferdinand":        "ferdinand_part_no",
    "reyher":           "reyher_part_no",
    "hopefix":          "hopefix_part_no",
    "fastbolt":         "fastbolt_part_no",
    "schafer":          "schaefer_part_no",
    "schaefer":         "schaefer_part_no",
    "king":             "kingb2b_part_no",
    "kingb2b":          "kingb2b_part_no",
    "wasi":             "wasishop_part_no",
    "wasishop":         "wasishop_part_no",
    "argip":            "argip_part_no",
    "vipa":             "vipa_part_no",
    "inoxmare":         "inoxmare_part_no",
}

_MAPPING_DB_COLS = [
    "gepcoop_part_no", "name",
    "irontrade_part_no", "csavarda_part_no", "koelner_part_no",
    "mekrs_part_no", "fabory_part_no", "ferdinand_part_no",
    "reyher_part_no", "hopefix_part_no", "fastbolt_part_no",
    "schaefer_part_no", "kingb2b_part_no", "wasishop_part_no",
    "argip_part_no", "vipa_part_no", "inoxmare_part_no",
]

_MAPPING_TEMPLATE_HEADERS = [
    "Cikkszám", "Cikknév",
    "Iron trade", "Csavarda", "Koelner", "Mekrs", "Fabory",
    "Ferdinand", "Reyher", "Hopefix", "Fastbolt", "Schafer",
    "King", "Wasi", "Argip", "Vipa", "Inoxmare",
]

_MAPPING_EMPTY = {"", "-", "–", "—", "N/A", "n/a"}

def _is_missing_mapping_columns(exc: Exception) -> bool:
    message = str(exc).lower()
    new_columns = ("argip_part_no", "vipa_part_no", "inoxmare_part_no")
    return any(column in message for column in new_columns) and (
        "does not exist" in message
        or "could not find" in message
        or "schema cache" in message
        or "column" in message
    )

def _normalize_mapping_header(col: str) -> str:
    stripped = str(col or "").strip()
    internal = _MAPPING_COL_ALIASES.get(stripped.lower(), stripped)
    return internal

def _normalize_mapping_columns(df) -> "pandas.DataFrame":
    """Rename human-readable / Hungarian column names to internal snake_case names."""
    rename = {col: _normalize_mapping_header(col) for col in df.columns}
    if rename:
        df = df.rename(columns=rename)
    return df

def _clean_mapping_val(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in _MAPPING_EMPTY else s

@app.post("/admin/upload-mapping")
async def admin_upload_mapping(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    import pandas as pd

    content  = await file.read()
    fname    = file.filename or ""
    ext      = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    if ext == "xlsx":
        df = pd.read_excel(io.BytesIO(content), dtype=str)
        df.columns = [c.strip() for c in df.columns]
        df = _normalize_mapping_columns(df)
        columns = list(df.columns)
        if "gepcoop_part_no" not in columns:
            raise HTTPException(
                status_code=400,
                detail="Az Excel-nek tartalmaznia kell a 'gepcoop_part_no' (vagy 'Gépcoop cikkszám') oszlopot.",
            )
        rows = df.fillna("").to_dict(orient="records")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        reader  = csv.DictReader(io.StringIO(text))
        source_columns = list(reader.fieldnames or [])
        columns = [_normalize_mapping_header(col) for col in source_columns]
        if "gepcoop_part_no" not in columns:
            raise HTTPException(
                status_code=400,
                detail="A CSV-nek tartalmaznia kell a 'gepcoop_part_no' vagy 'Cikkszám' oszlopot.",
            )
        rows = [
            {
                _normalize_mapping_header(col): value
                for col, value in dict(raw_row).items()
            }
            for raw_row in reader
        ]

    if not rows:
        raise HTTPException(status_code=400, detail="A fájl üres.")

    # ── Supabase upsert ────────────────────────────────────────────
    supabase_rows = 0
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    if sb_url and sb_key:
        from supabase import create_client
        sb = create_client(sb_url, sb_key)
        TABLE      = "article_mapping"
        BATCH_SIZE = 500

        def _build_sb_rows(raw_rows: list[dict]) -> list[dict]:
            result = []
            for r in raw_rows:
                part_no = _clean_mapping_val(r.get("gepcoop_part_no", ""))
                if not part_no:
                    continue
                result.append({col: _clean_mapping_val(r.get(col, "")) for col in _MAPPING_DB_COLS})
            return result

        sb_rows = await asyncio.to_thread(_build_sb_rows, rows)

        def _do_upsert(sb_rows: list[dict]) -> int:
            # Validate the expanded schema before deleting the current mapping.
            sb.table(TABLE).select(",".join(_MAPPING_DB_COLS)).limit(1).execute()
            # Full replace: delete all then batch upsert
            sb.table(TABLE).delete().neq("gepcoop_part_no", "").execute()
            total = len(sb_rows)
            batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            for i in range(batches):
                batch = sb_rows[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
                sb.table(TABLE).upsert(batch, on_conflict="gepcoop_part_no").execute()
                log.info(f"Supabase upsert batch {i+1}/{batches} ({len(batch)} rows)")
            return total

        try:
            supabase_rows = await asyncio.to_thread(_do_upsert, sb_rows)
        except Exception as exc:
            if _is_missing_mapping_columns(exc):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Az article_mapping tábla új webshoposzlopai hiányoznak. "
                        "Futtasd a deploy/supabase_article_mapping_new_suppliers.sql migrációt."
                    ),
                )
            raise
        log.info(f"Supabase mapping frissítve: {supabase_rows} sor")
    else:
        log.warning("SUPABASE_URL/KEY hiányzik — csak lokális CSV mentve")

    log.info(f"Mapping frissítve: {len(rows)} sor, oszlopok={columns}, fájl={fname}")
    return {"filename": fname, "columns": columns, "rows": rows, "supabase_rows": supabase_rows}


# ── Gép-Coop own stock (independent table: gepcoop_stock) ──────────────
GEPCOOP_STOCK_TABLE = "gepcoop_stock"
_GEPCOOP_STOCK_COLS = [
    "part_no", "name", "sellable",
    "last_purchase_price", "last_purchase_date", "selling_price",
]


def _norm_header(col: str) -> str:
    """Lowercase, trim, drop dots and collapse spaces for header matching."""
    return " ".join(str(col or "").strip().lower().replace(".", "").split())


def _slug_header(col: str) -> str:
    """Normalize arbitrary Excel headers to a loose slug for alias matching."""
    text = str(col or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_GEPCOOP_COL_ALIASES: dict[str, str] = {
    "cikkszám": "part_no", "cikkszam": "part_no",
    "gépcoop cikkszám": "part_no", "gepcoop cikkszam": "part_no",
    "cikknév": "name", "cikknev": "name", "megnevezés": "name", "megnevezes": "name",
    "eladható": "sellable", "eladhato": "sellable",
    "eladható mennyiség": "sellable", "eladhato mennyiseg": "sellable",
    "telephelyi utolsó besz ár": "last_purchase_price",
    "telephelyi utolso besz ar": "last_purchase_price",
    "utolsó beszerzési ár": "last_purchase_price", "utolso beszerzesi ar": "last_purchase_price",
    "telephelyi utolsó besz dátum": "last_purchase_date",
    "telephelyi utolso besz datum": "last_purchase_date",
    "utolsó beszerzési dátum": "last_purchase_date", "utolso beszerzesi datum": "last_purchase_date",
    "eladási ár": "selling_price", "eladasi ar": "selling_price",
}


def _is_missing_gepcoop_table(exc: Exception) -> bool:
    msg = str(exc).lower()
    return GEPCOOP_STOCK_TABLE in msg and (
        "does not exist" in msg or "not exist" in msg
        or "could not find the table" in msg or "schema cache" in msg
        or "relation" in msg
    )


ARGIP_PRICE_TABLE = "argip_price_list"
_ARGIP_PRICE_COLS = [
    "ean_code",
    "ean_code_line",
    "list_name",
    "argip_part_no",
    "description_pl",
    "description_en",
    "customer_description",
    "customer_part_no",
    "hs_code",
    "country_of_origin",
    "discount_pct",
    "base_price_eur",
    "price_lvl_1_eur",
    "moq_lvl_1_pcs",
    "price_lvl_2_eur",
    "moq_lvl_2_pcs",
    "box_quantity_pcs",
    "box_weight_kg",
    "asortyment_id",
    "asortyment_bazowy_id",
    "stan_kart_spec_pcs",
    "stock_pcs",
    "dose",
    "sku",
]

_ARGIP_COL_ALIASES: dict[str, str] = {
    "argip ean code": "ean_code",
    "ean code": "ean_code",
    "ean": "ean_code",
    "ean code line": "ean_code_line",
    "list name": "list_name",
    "argip part": "argip_part_no",
    "argip part #": "argip_part_no",
    "argip description pl": "description_pl",
    "argip description en": "description_en",
    "customer description": "customer_description",
    "customer part": "customer_part_no",
    "customer part #": "customer_part_no",
    "hs code": "hs_code",
    "country of origin": "country_of_origin",
    "discount with b2b disc incl": "discount_pct",
    "discount b2b disc incl": "discount_pct",
    "base price eur": "base_price_eur",
    "price lvl 1 eur": "price_lvl_1_eur",
    "moq lvl1 pcs": "moq_lvl_1_pcs",
    "moq lvl 1 pcs": "moq_lvl_1_pcs",
    "price lvl 2 eur": "price_lvl_2_eur",
    "moq lvl 2 pcs": "moq_lvl_2_pcs",
    "box quantity pcs": "box_quantity_pcs",
    "box weight kg": "box_weight_kg",
    "asortymentid": "asortyment_id",
    "asortymentbazowyid": "asortyment_bazowy_id",
    "stan kart spec szt": "stan_kart_spec_pcs",
    "stock pcs": "stock_pcs",
    "dose": "dose",
    "sku": "sku",
}


def _parse_decimal_or_none(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _MAPPING_EMPTY:
        return None
    text = text.replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int_or_none(value) -> int | None:
    parsed = _parse_decimal_or_none(value)
    if parsed is None:
        return None
    return int(parsed)


def _clean_text_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text in _MAPPING_EMPTY else text


def _normalize_argip_columns(df) -> "pandas.DataFrame":
    rename = {}
    for col in df.columns:
        target = _ARGIP_COL_ALIASES.get(_slug_header(col))
        if target:
            rename[col] = target
    if rename:
        df = df.rename(columns=rename)
    return df


def _build_argip_rows(raw_rows: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for raw in raw_rows:
        row = {
            "ean_code":             _clean_text_or_none(raw.get("ean_code")),
            "ean_code_line":        _clean_text_or_none(raw.get("ean_code_line")),
            "list_name":            _clean_text_or_none(raw.get("list_name")),
            "argip_part_no":        _clean_text_or_none(raw.get("argip_part_no")),
            "description_pl":       _clean_text_or_none(raw.get("description_pl")),
            "description_en":       _clean_text_or_none(raw.get("description_en")),
            "customer_description": _clean_text_or_none(raw.get("customer_description")),
            "customer_part_no":     _clean_text_or_none(raw.get("customer_part_no")),
            "hs_code":              _clean_text_or_none(raw.get("hs_code")),
            "country_of_origin":    _clean_text_or_none(raw.get("country_of_origin")),
            "discount_pct":         _parse_decimal_or_none(raw.get("discount_pct")),
            "base_price_eur":       _parse_decimal_or_none(raw.get("base_price_eur")),
            "price_lvl_1_eur":      _parse_decimal_or_none(raw.get("price_lvl_1_eur")),
            "moq_lvl_1_pcs":        _parse_int_or_none(raw.get("moq_lvl_1_pcs")),
            "price_lvl_2_eur":      _parse_decimal_or_none(raw.get("price_lvl_2_eur")),
            "moq_lvl_2_pcs":        _parse_int_or_none(raw.get("moq_lvl_2_pcs")),
            "box_quantity_pcs":     _parse_int_or_none(raw.get("box_quantity_pcs")),
            "box_weight_kg":        _parse_decimal_or_none(raw.get("box_weight_kg")),
            "asortyment_id":        _clean_text_or_none(raw.get("asortyment_id")),
            "asortyment_bazowy_id": _clean_text_or_none(raw.get("asortyment_bazowy_id")),
            "stan_kart_spec_pcs":   _clean_text_or_none(raw.get("stan_kart_spec_pcs")),
            "stock_pcs":            _parse_int_or_none(raw.get("stock_pcs")),
            "dose":                 _parse_decimal_or_none(raw.get("dose")),
            "sku":                  _clean_text_or_none(raw.get("sku")),
        }
        if row["ean_code"]:   # EAN code is now the primary key
            rows.append(row)
    return rows


def _is_missing_argip_table(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ARGIP_PRICE_TABLE in msg and (
        "does not exist" in msg
        or "not exist" in msg
        or "could not find the table" in msg
        or "schema cache" in msg
        or "relation" in msg
    )


@app.post("/admin/gepcoop-stock/upload")
async def admin_upload_gepcoop_stock(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Replace the whole Gép-Coop stock table from an uploaded Excel/CSV."""
    _get_admin(authorization)
    content = await file.read()
    fname = file.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    if ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
        headers = [str(c) for c in df.columns]
        raw_rows = df.to_dict(orient="records")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        headers = list(reader.fieldnames or [])
        raw_rows = [dict(r) for r in reader]

    # Map source headers → internal columns (Hungarian aliases or snake_case).
    colmap: dict[str, str] = {}
    for h in headers:
        target = _GEPCOOP_COL_ALIASES.get(_norm_header(h))
        if not target and h in _GEPCOOP_STOCK_COLS:
            target = h
        if target:
            colmap[h] = target

    if "part_no" not in colmap.values():
        raise HTTPException(
            status_code=400,
            detail="A fájlnak tartalmaznia kell a 'Cikkszám' oszlopot.",
        )

    def _clean(v):
        if v is None:
            return None
        s = str(v).strip()
        return None if s in _MAPPING_EMPTY else s

    built: list[dict] = []
    for r in raw_rows:
        row = {col: None for col in _GEPCOOP_STOCK_COLS}
        for h, col in colmap.items():
            row[col] = _clean(r.get(h, ""))
        if row.get("part_no"):
            built.append(row)

    if not built:
        raise HTTPException(status_code=400, detail="A fájl nem tartalmaz érvényes sort (hiányzó Cikkszám?).")

    sb = _get_supabase_main()
    if sb is None:
        raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva.")

    BATCH = 500

    def _replace(rows_in: list[dict]) -> int:
        sb.table(GEPCOOP_STOCK_TABLE).delete().neq("part_no", "").execute()
        total = len(rows_in)
        for i in range((total + BATCH - 1) // BATCH):
            batch = rows_in[i * BATCH:(i + 1) * BATCH]
            sb.table(GEPCOOP_STOCK_TABLE).upsert(batch, on_conflict="part_no").execute()
            log.info(f"Gépcoop stock upsert batch {i+1} ({len(batch)} sor)")
        return total

    try:
        n = await asyncio.to_thread(_replace, built)
    except Exception as exc:
        if _is_missing_gepcoop_table(exc):
            raise HTTPException(
                status_code=503,
                detail="A 'gepcoop_stock' tábla hiányzik. Futtasd a deploy/supabase_gepcoop_stock.sql migrációt a Supabase-ben.",
            )
        raise HTTPException(status_code=500, detail=f"Gép-Coop készlet feltöltése sikertelen: {exc}")

    log.info(f"Gép-Coop készlet frissítve: {n} sor, fájl={fname}")
    return {"filename": fname, "rows": n, "columns": _GEPCOOP_STOCK_COLS}


@app.get("/admin/gepcoop-stock")
def admin_get_gepcoop_stock(authorization: str | None = Header(default=None)):
    """Preview the Gép-Coop stock table (row count + first rows) for the admin."""
    _get_admin(authorization)
    sb = _get_supabase_main()
    base = {"columns": _GEPCOOP_STOCK_COLS, "rows": [], "count": 0}
    if sb is None:
        return base
    try:
        res = sb.table(GEPCOOP_STOCK_TABLE).select("*").order("part_no").limit(15).execute()
        rows = res.data or []
        cnt = sb.table(GEPCOOP_STOCK_TABLE).select("part_no", count="exact").limit(1).execute()
        count = cnt.count if cnt.count is not None else len(rows)
    except Exception as exc:
        if _is_missing_gepcoop_table(exc):
            return {**base, "table_missing": True}
        log.warning(f"Gép-Coop készlet előnézet sikertelen: {exc}")
        return base
    return {"columns": _GEPCOOP_STOCK_COLS, "rows": rows, "count": count}


@app.get("/gepcoop/stock")
def get_gepcoop_stock(
    internal_part_no: str,
    authorization: str | None = Header(default=None),
):
    """Return the Gép-Coop stock row for a part (any logged-in user). Always 200."""
    _get_username(authorization)
    part = internal_part_no.strip()
    sb = _get_supabase_main()
    if sb is None or not part:
        return {"found": False, "row": None, "part_no": part}
    try:
        res = sb.table(GEPCOOP_STOCK_TABLE).select("*").ilike("part_no", part).limit(1).execute()
    except Exception as exc:
        if _is_missing_gepcoop_table(exc):
            return {"found": False, "row": None, "part_no": part, "table_missing": True}
        log.warning(f"Gép-Coop készlet lekérdezés sikertelen: {exc}")
        return {"found": False, "row": None, "part_no": part}
    row = (res.data or [None])[0]
    return {"found": bool(row), "row": row, "part_no": part}


@app.post("/admin/argip-price/upload")
async def admin_upload_argip_price(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Replace the Argip price table from the uploaded Excel/CSV file."""
    _get_admin(authorization)
    content = await file.read()
    fname = file.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    if ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
        df.columns = [str(c).strip() for c in df.columns]
        df = _normalize_argip_columns(df)
        source_rows = df.to_dict(orient="records")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw_row in reader:
            row = {}
            for col, value in dict(raw_row).items():
                target = _ARGIP_COL_ALIASES.get(_slug_header(col))
                row[target or col] = value
            rows.append(row)
        source_rows = rows

    if not source_rows:
        raise HTTPException(status_code=400, detail="A fájl üres.")

    argip_rows = _build_argip_rows(source_rows)
    if not argip_rows:
        raise HTTPException(
            status_code=400,
            detail="A fájl nem tartalmaz érvényes 'Argip part #' oszlopot vagy kitöltött Argip cikkszámot.",
        )

    sb = _get_supabase_main()
    if sb is None:
        raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva.")

    BATCH = 500

    def _replace(rows_in: list[dict]) -> int:
        sb.table(ARGIP_PRICE_TABLE).select(",".join(_ARGIP_PRICE_COLS)).limit(1).execute()
        sb.table(ARGIP_PRICE_TABLE).delete().neq("argip_part_no", "").execute()
        total = len(rows_in)
        for i in range((total + BATCH - 1) // BATCH):
            batch = rows_in[i * BATCH:(i + 1) * BATCH]
            sb.table(ARGIP_PRICE_TABLE).upsert(batch, on_conflict="ean_code").execute()
            log.info(f"Argip árlista upsert batch {i+1} ({len(batch)} sor)")
        return total

    try:
        count = _replace(argip_rows)
    except Exception as exc:
        if _is_missing_argip_table(exc):
            raise HTTPException(
                status_code=503,
                detail="Az 'argip_price_list' tábla hiányzik. Futtasd a deploy/supabase_argip_price_list.sql migrációt a Supabase-ben.",
            )
        raise HTTPException(status_code=500, detail=f"Argip árlista feltöltése sikertelen: {exc}")

    log.info(f"Argip árlista frissítve: {count} sor, fájl={fname}")
    return {"filename": fname, "rows": count, "columns": _ARGIP_PRICE_COLS}


@app.get("/admin/argip-price")
def admin_get_argip_price(
    authorization: str | None = Header(default=None),
):
    """Preview the uploaded Argip price list for the admin."""
    _get_admin(authorization)
    sb = _get_supabase_main()
    base = {"columns": _ARGIP_PRICE_COLS, "rows": [], "count": 0}
    if sb is None:
        return base
    try:
        res = sb.table(ARGIP_PRICE_TABLE).select(",".join(_ARGIP_PRICE_COLS)).order("argip_part_no").limit(15).execute()
        rows = res.data or []
        cnt = sb.table(ARGIP_PRICE_TABLE).select("argip_part_no", count="exact").limit(1).execute()
        count = cnt.count if cnt.count is not None else len(rows)
    except Exception as exc:
        if _is_missing_argip_table(exc):
            return {**base, "table_missing": True}
        log.warning(f"Argip árlista előnézet sikertelen: {exc}")
        return base
    return {"columns": _ARGIP_PRICE_COLS, "rows": rows, "count": count}


@app.get("/admin/mapping-template")
def admin_mapping_template(
    token: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Return an Excel (.xlsx) template with all column headers and two example rows.
    The template is not sensitive, so any valid user token is accepted."""
    if token:
        _get_username_from_token(token)
    else:
        _get_username(authorization)
    import pandas as pd

    example_rows = [
        ["GC001", "Hatlapfejű csavar DIN 933 M8x20 horg.", "", "934012000000801000", "61025", "08555.18.02.100.100", "", "", "000094001000050112", "", "", "", "", "", "", "", ""],
        ["GC002", "Hatlapfejű csavar DIN 931 M10x50 horg.", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]
    df = pd.DataFrame(example_rows, columns=_MAPPING_TEMPLATE_HEADERS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Mapping")
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mapping_sablon.xlsx"},
    )


@app.delete("/admin/mapping")
def admin_delete_mapping(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    from agent.tools import _get_supabase
    sb = _get_supabase()
    if sb is None:
        raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva.")
    try:
        sb.table("article_mapping").delete().neq("gepcoop_part_no", "").execute()
        log.info("Supabase article_mapping tábla törölve")
    except Exception as exc:
        log.warning(f"Supabase törlés sikertelen: {exc}")
        raise HTTPException(status_code=500, detail=f"Supabase törlés sikertelen: {exc}")
    return {"deleted": True}


@app.get("/admin/runs")
def admin_get_runs(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    sb = _get_supabase_main()
    if sb is None:
        return {"runs": []}
    try:
        select_cols = (
            "run_id,username,gepcoop_part_no,started_at,finished_at,status,"
            "suppliers_queried,suppliers_ok,suppliers_error,error_message,duration_ms,"
            "recommendation_feedback,recommendation_feedback_at,recommendation_feedback_by"
        )
        try:
            res = (
                sb.table("query_runs")
                .select(select_cols)
                .order("started_at", desc=True)
                .limit(10)
                .execute()
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "username" not in msg and not _is_missing_feedback_columns(exc):
                raise
            res = (
                sb.table("query_runs")
                .select("run_id,gepcoop_part_no,started_at,finished_at,status,suppliers_queried,suppliers_ok,suppliers_error,error_message,duration_ms")
                .order("started_at", desc=True)
                .limit(10)
                .execute()
            )
        return {"runs": res.data or []}
    except Exception as exc:
        log.warning(f"admin_get_runs hiba: {exc}")
        return {"runs": []}


@app.get("/admin/fx-settings")
def admin_get_fx_settings(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    return {
        "eur_to_huf_rate": round(_get_manual_eur_huf_rate(), 4),
        "source": "admin",
    }


@app.post("/admin/fx-settings")
def admin_update_fx_settings(
    req: UpdateFxSettingsRequest,
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    rate = float(req.eur_to_huf_rate)
    if rate <= 0:
        raise HTTPException(status_code=400, detail="Az EUR → HUF árfolyamnak pozitív számnak kell lennie.")
    normalized = f"{rate:.4f}".rstrip("0").rstrip(".")
    _update_env_file({"EUR_TO_HUF_RATE": normalized})
    log.info("Admin EUR árfolyam frissítve: 1 EUR = %s HUF", normalized)
    return {
        "eur_to_huf_rate": round(float(normalized), 4),
        "saved": True,
    }


@app.get("/admin/runs/export")
def admin_export_runs(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    sb = _get_supabase_main()
    if sb is None:
        raise HTTPException(status_code=503, detail="Supabase nincs konfigurálva.")

    select_cols = (
        "run_id,username,gepcoop_part_no,started_at,finished_at,status,"
        "suppliers_queried,suppliers_ok,suppliers_error,error_message,duration_ms,"
        "recommendation_feedback,recommendation_feedback_at,recommendation_feedback_by"
    )
    try:
        try:
            res = (
                sb.table("query_runs")
                .select(select_cols)
                .order("started_at", desc=True)
                .limit(5000)
                .execute()
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "username" not in msg and not _is_missing_feedback_columns(exc):
                raise
            res = (
                sb.table("query_runs")
                .select("run_id,gepcoop_part_no,started_at,finished_at,status,suppliers_queried,suppliers_ok,suppliers_error,error_message,duration_ms")
                .order("started_at", desc=True)
                .limit(5000)
                .execute()
            )

        rows = []
        for row in res.data or []:
            rows.append({
                "Run ID": row.get("run_id", ""),
                "User": row.get("username", ""),
                "Cikkszám": row.get("gepcoop_part_no", ""),
                "Indítva": row.get("started_at", ""),
                "Befejezve": row.get("finished_at", ""),
                "Státusz": row.get("status", ""),
                "Idő (ms)": row.get("duration_ms", ""),
                "Javaslat feedback": row.get("recommendation_feedback", "") or "",
                "Feedback időpont": row.get("recommendation_feedback_at", "") or "",
                "Feedback user": row.get("recommendation_feedback_by", "") or "",
                "Lekérdezett beszállítók": ", ".join(row.get("suppliers_queried") or []),
                "Sikeres beszállítók": ", ".join(row.get("suppliers_ok") or []),
                "Hibás beszállítók": ", ".join(row.get("suppliers_error") or []),
                "Hibaüzenet": row.get("error_message", "") or "",
            })

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            pd.DataFrame(rows).to_excel(writer, sheet_name="Futásnapló", index=False)
        buf.seek(0)
        filename = f"futasnaplo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as exc:
        log.warning(f"admin_export_runs hiba: {exc}")
        raise HTTPException(status_code=500, detail=f"Export sikertelen: {exc}")


@app.get("/admin/runs/chart")
def admin_get_runs_chart(
    range: str = "week",
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    sb = _get_supabase_main()
    if sb is None:
        return {"range": range, "runs": []}

    try:
        query = (
            sb.table("query_runs")
            .select("run_id,gepcoop_part_no,started_at,status,duration_ms")
            .order("started_at", desc=False)
        )

        now_utc = datetime.now(timezone.utc)
        if range == "week":
            query = query.gte("started_at", (now_utc - timedelta(days=7)).isoformat())
        elif range == "month":
            query = query.gte("started_at", (now_utc - timedelta(days=30)).isoformat())
        elif range != "all":
            raise HTTPException(status_code=400, detail="Érvénytelen időintervallum.")

        res = query.limit(500).execute()
        runs = []
        for row in res.data or []:
            duration_ms = row.get("duration_ms")
            if duration_ms is None:
                continue
            runs.append({
                "run_id": row.get("run_id"),
                "gepcoop_part_no": row.get("gepcoop_part_no"),
                "started_at": row.get("started_at"),
                "status": row.get("status"),
                "duration_ms": duration_ms,
                "duration_sec": round(float(duration_ms) / 1000, 2),
            })
        return {"range": range, "runs": runs}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning(f"admin_get_runs_chart hiba: {exc}")
        return {"range": range, "runs": []}


@app.get("/admin/suppliers")
def admin_get_suppliers(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    result = []
    info_by_supplier = _supplier_info_items_for(list(SUPPLIER_CREDS.keys()))
    for sid, creds in SUPPLIER_CREDS.items():
        meta = SUPPLIER_META.get(sid, {})
        entry = {
            "id":       sid,
            "url":      creds.get("url", ""),
            "username": creds.get("username", ""),
            "otp":      meta.get("otp", False),
            "info_items": info_by_supplier.get(sid, []),
            "extra":    [
                {"key": ex["key"], "label": ex["label"], "value": creds.get(ex["key"], "")}
                for ex in meta.get("extra", [])
            ],
        }
        result.append(entry)
    return {"suppliers": result}


@app.post("/admin/update-supplier")
def admin_update_supplier(
    req: UpdateSupplierRequest,
    authorization: str | None = Header(default=None),
):
    admin_username = _get_admin(authorization)
    if req.supplier_id not in SUPPLIER_CREDS:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {req.supplier_id}")
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="Felhasználónév megadása kötelező.")
    meta       = SUPPLIER_META[req.supplier_id]
    env_prefix = meta["env"]

    SUPPLIER_CREDS[req.supplier_id]["username"] = req.username.strip()
    env_updates = {
        f"{env_prefix}_USERNAME": req.username.strip(),
    }
    if req.password:
        SUPPLIER_CREDS[req.supplier_id]["password"] = req.password
        env_updates[f"{env_prefix}_PASSWORD"] = req.password
    # Save extra fields (customer_code, shortname, …)
    for ex in meta.get("extra", []):
        val = (req.extra or {}).get(ex["key"], "")
        SUPPLIER_CREDS[req.supplier_id][ex["key"]] = val
        env_updates[f"{env_prefix}_{ex['env_suffix']}"] = val

    _update_env_file(env_updates)
    _apply_suppliers_to_env(SUPPLIER_CREDS)
    saved_info_items = None
    if req.info_items is not None:
        saved_info_items = _replace_supplier_info_items(req.supplier_id, req.info_items, admin_username)
    log.info(f"Beszállítói adatok frissítve: {req.supplier_id}, username={req.username.strip()}")
    return {
        "supplier_id": req.supplier_id,
        "username": req.username.strip(),
        "info_items": saved_info_items if saved_info_items is not None else _supplier_info_items_for([req.supplier_id]).get(req.supplier_id, []),
    }


@app.post("/admin/supplier-info")
def admin_update_supplier_info(
    req: UpdateSupplierInfoRequest,
    authorization: str | None = Header(default=None),
):
    admin_username = _get_admin(authorization)
    supplier_id = req.supplier_id.strip().lower()
    if supplier_id not in SUPPLIER_CREDS:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {supplier_id}")
    saved_info_items = _replace_supplier_info_items(supplier_id, req.info_items, admin_username)
    log.info(f"Beszállítói rendelési információk frissítve: {supplier_id}")
    return {
        "supplier_id": supplier_id,
        "info_items": saved_info_items,
    }


@app.get("/admin/users")
def admin_get_users(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    _ensure_auth_bootstrap()
    users = [
        {
            **_auth_row_to_user(row),
            "is_primary": _is_primary_admin_user(row.get("username", "")),
            "protected": _is_primary_admin_user(row.get("username", "")),
        }
        for row in _get_auth_users(include_inactive=False)
        if not _is_primary_admin_user(row.get("username", ""))
    ]
    return {"users": users}


@app.get("/admin/app-user")
def admin_get_app_user(
    authorization: str | None = Header(default=None),
):
    admin_username = _get_admin(authorization)
    _ensure_auth_bootstrap()
    row = _get_auth_user(admin_username, include_inactive=False)
    if not row:
        raise HTTPException(status_code=404, detail="Az admin felhasználó nem található.")
    return _auth_row_to_user(row)


@app.post("/admin/update-app-user")
def admin_update_app_user(
    req: UpdateAppUserRequest,
    authorization: str | None = Header(default=None),
):
    admin_username = _get_admin(authorization)
    _ensure_auth_bootstrap()
    if not req.password:
        raise HTTPException(status_code=400, detail="Új jelszó megadása kötelező.")

    row = _get_auth_user(admin_username, include_inactive=True)
    if not row:
        raise HTTPException(status_code=404, detail="Az admin felhasználó nem található.")

    _set_auth_password(admin_username, req.password)
    _invalidate_user_sessions(admin_username)
    log.info(f"Admin saját jelszó frissítve: username={admin_username}")
    return {"username": admin_username}


@app.post("/admin/users")
def admin_create_user(
    req: CreateUserRequest,
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    _ensure_auth_bootstrap()
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Felhasználónév és jelszó megadása kötelező.")
    if _get_auth_user(username, include_inactive=True):
        raise HTTPException(status_code=400, detail="Ez a felhasználónév már létezik.")
    _upsert_auth_user(
        username,
        req.password,
        is_admin=bool(req.is_admin),
        is_primary=False,
        is_active=True,
    )
    log.info(f"Új felhasználó létrehozva: username={username}")
    return {"username": username, "is_admin": bool(req.is_admin)}


@app.post("/admin/update-user-password")
def admin_update_user_password(
    req: UpdateUserRequest,
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    _ensure_auth_bootstrap()
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Felhasználónév és jelszó megadása kötelező.")

    user = _get_auth_user(username, include_inactive=True)
    if not user:
        raise HTTPException(status_code=404, detail="Ismeretlen felhasználó.")
    _set_auth_password(username, req.password)
    _invalidate_user_sessions(username)
    log.info(f"Felhasználói jelszó frissítve: username={username}")
    return {"username": username}


@app.post("/admin/users/{username}/admin")
def admin_set_user_admin(
    username: str,
    req: UpdateUserRoleRequest,
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    _ensure_auth_bootstrap()
    username = username.strip()
    user = _get_auth_user(username, include_inactive=True)
    if not user:
        raise HTTPException(status_code=404, detail="Ismeretlen felhasználó.")
    if _is_primary_admin_user(username):
        raise HTTPException(status_code=400, detail="A fő admin felhasználó mindig admin.")
    _set_auth_admin(username, bool(req.is_admin))
    _invalidate_user_sessions(username)
    log.info(f"Felhasználó admin joga frissítve: username={username}, is_admin={bool(req.is_admin)}")
    return {"username": username, "is_admin": bool(req.is_admin)}


@app.delete("/admin/users/{username}")
def admin_delete_user(
    username: str,
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    _ensure_auth_bootstrap()
    username = username.strip()
    user = _get_auth_user(username, include_inactive=True)
    if not user:
        raise HTTPException(status_code=404, detail="Ismeretlen felhasználó.")
    if _is_primary_admin_user(username):
        raise HTTPException(status_code=400, detail="A fő admin felhasználó nem törölhető.")
    _soft_delete_auth_user(username)
    _invalidate_user_sessions(username)
    log.info(f"Felhasználó törölve: username={username}")
    return {"username": username}
