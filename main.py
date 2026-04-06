import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from pathlib import Path
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

UI_FILE   = Path(__file__).parent / "ui" / "index.html"
LOGO_FILE = Path(__file__).parent / "assets" / "logo.png"
GUIDE_PDF_FILE = Path(__file__).parent / "Gep-Coop-Kft-Belso-hasznalatra-or-2026.pdf"
GUIDE_STORAGE_BUCKET = os.environ.get("SUPABASE_GUIDE_BUCKET", "internal-docs").strip() or "internal-docs"
GUIDE_STORAGE_PATH = os.environ.get("SUPABASE_GUIDE_PATH", "guide/current.pdf").strip() or "guide/current.pdf"


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


def _guide_storage_bucket(create_if_missing: bool = False):
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
                if GUIDE_STORAGE_BUCKET not in bucket_names:
                    sb.storage.create_bucket(
                        GUIDE_STORAGE_BUCKET,
                        options={"public": False, "allowed_mime_types": ["application/pdf"]},
                    )
                    log.info(f"Guide storage bucket létrehozva: {GUIDE_STORAGE_BUCKET}")
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    raise
        return sb.storage.from_(GUIDE_STORAGE_BUCKET)
    except Exception as exc:
        log.warning(f"Guide storage bucket elérése sikertelen: {exc}")
        return None


def _read_guide_pdf_bytes() -> bytes | None:
    bucket = _guide_storage_bucket(create_if_missing=False)
    if bucket is not None:
        try:
            if bucket.exists(GUIDE_STORAGE_PATH):
                return bucket.download(GUIDE_STORAGE_PATH)
        except Exception as exc:
            log.warning(f"Guide PDF olvasása Storage-ból sikertelen: {exc}")
    if GUIDE_PDF_FILE.exists():
        content = GUIDE_PDF_FILE.read_bytes()
        if bucket is not None:
            try:
                bucket.upload(
                    GUIDE_STORAGE_PATH,
                    content,
                    {"content-type": "application/pdf", "upsert": "true"},
                )
                log.info(f"Guide PDF automatikusan feltöltve Storage-ba: {GUIDE_STORAGE_BUCKET}/{GUIDE_STORAGE_PATH}")
            except Exception as exc:
                log.warning(f"Guide PDF automatikus Storage feltöltése sikertelen: {exc}")
        return content
    return None


def _write_guide_pdf_bytes(content: bytes) -> None:
    bucket = _guide_storage_bucket(create_if_missing=True)
    if bucket is None:
        raise RuntimeError("A Supabase Storage nem érhető el a dokumentum mentéséhez.")
    try:
        bucket.upload(
            GUIDE_STORAGE_PATH,
            content,
            {"content-type": "application/pdf", "upsert": "true"},
        )
    except Exception as exc:
        raise RuntimeError(f"A dokumentum Storage mentése sikertelen: {exc}") from exc
    try:
        GUIDE_PDF_FILE.write_bytes(content)
    except Exception as exc:
        log.warning(f"Helyi guide PDF tükör mentése sikertelen: {exc}")


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
    All suppliers are ranked together. Non-HUF suppliers (e.g. mekrs, reyher)
    are included using their live-converted price_per_db_huf value.
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
            f" ≈ {_hu(huf_price)} HUF/db (1 {curr} = {_hu(rate, 2)} HUF, open.er-api.com)"
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

class UpdateSupplierRequest(BaseModel):
    supplier_id: str
    username: str
    password: str = ""
    extra: dict | None = None

class UpdatePasswordRequest(BaseModel):
    supplier_id: str
    password: str


# ── Routes ────────────────────────────────────────────────────────────
@app.get("/")
def serve_ui():
    return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})


@app.get("/logo.png")
def serve_logo():
    return FileResponse(LOGO_FILE, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/guide/pdf")
def serve_guide_pdf(
    download: int = 0,
    token: str | None = None,
    authorization: str | None = Header(default=None),
):
    if token:
        _get_username_from_token(token)
    else:
        _get_username(authorization)
    content = _read_guide_pdf_bytes()
    if not content:
        raise HTTPException(status_code=404, detail="Guide PDF not found")
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'{disposition}; filename="{GUIDE_PDF_FILE.name}"',
        },
    )


@app.post("/admin/guide/upload")
async def admin_upload_guide_pdf(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    admin_username = _get_admin(authorization)
    if not _is_primary_admin_user(admin_username):
        raise HTTPException(status_code=403, detail="Csak a fő admin cserélheti a dokumentumot.")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Fájl megadása kötelező.")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Csak PDF fájl tölthető fel.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Az üres fájl nem tölthető fel.")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="A feltöltött fájl nem érvényes PDF.")

    _write_guide_pdf_bytes(content)
    log.info(f"Guide PDF frissítve: uploaded_by={admin_username}, filename={file.filename}")
    return {
        "filename": GUIDE_PDF_FILE.name,
        "uploaded_by": admin_username,
        "storage_bucket": GUIDE_STORAGE_BUCKET,
        "storage_path": GUIDE_STORAGE_PATH,
    }


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

    found_ids = {s["supplier_id"] for s in suppliers}
    unavailable = [
        {"supplier_id": sid, "supplier_url": meta["url"]}
        for sid, meta in SUPPLIER_META.items()
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


def _save_run(run_id, part_no, started_at, status,
              suppliers_queried, suppliers_ok, suppliers_error,
              error_message, duration_ms):
    sb = _get_supabase_main()
    if sb is None:
        return
    try:
        sb.table("query_runs").upsert({
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
        }, on_conflict="run_id").execute()
    except Exception as exc:
        log.warning(f"run log mentés sikertelen: {exc}")


@app.get("/query/stream")
async def query_stream(
    internal_part_no: str,
    suppliers: str | None = None,
    authorization: str | None = Header(default=None),
):
    _get_username(authorization)

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
                        # Any supplier login failure → prompt user to update password
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

            await queue.put(("result", {
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
                _error_message, duration_ms,
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


# Search URL templates used by /supplier/open for non-session suppliers
_SUPPLIER_SEARCH_URLS: dict[str, str] = {
    "csavarda":  "https://csavarda.hu/pest/kereso?search={part_no}",
    "irontrade": "https://irontrade.hu/kereso?name={part_no}",
    "fabory":    "https://www.fabory.com/hu/search?text={part_no}",
    "fastbolt":  "https://fbonline.fastbolt.com/matrix/{part_no}",
    "mekrs":     "https://eshop.mekrs.cz/en",
    "hopefix":   "https://www.hopefix.cz/en",
    "schaefer":  "https://shop.schaefer-peters.com/sp/en/login/",
    "kingb2b":   "https://kingb2b.it/PORTAL/",
    "wasishop":  "https://www.wasishop.de",
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
    Open a supplier's website for the buyer.
    Always return a URL so the frontend can open it in a new tab.
    Any webshop login is handled manually by the user in their own browser.
    """
    _get_username(authorization)
    data = await req.json()
    sid              = (data.get("supplier_id") or "").strip().lower()
    supplier_part_no = (data.get("supplier_part_no") or "").strip()

    if sid not in SUPPLIER_META:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {sid}")

    url = _build_supplier_url(sid, supplier_part_no)
    return {"status": "redirect", "url": url}


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
    "gépcoop cikkszám": "gepcoop_part_no",
    "gepcoop cikkszám": "gepcoop_part_no",
    "cikknév":          "name",
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
}

_MAPPING_EMPTY = {"", "-", "–", "—", "N/A", "n/a"}

def _normalize_mapping_columns(df) -> "pandas.DataFrame":
    """Rename human-readable / Hungarian column names to internal snake_case names."""
    import pandas as pd
    rename = {}
    for col in df.columns:
        low = col.strip().lower()
        if low in _MAPPING_COL_ALIASES:
            rename[col] = _MAPPING_COL_ALIASES[low]
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
        columns = list(reader.fieldnames or [])
        if "gepcoop_part_no" not in columns:
            raise HTTPException(
                status_code=400,
                detail="A CSV-nek tartalmaznia kell a 'gepcoop_part_no' oszlopot.",
            )
        rows = [dict(r) for r in reader]

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
        DB_COLS    = [
            "gepcoop_part_no", "name",
            "csavarda_part_no", "irontrade_part_no", "koelner_part_no",
            "mekrs_part_no", "fabory_part_no", "ferdinand_part_no",
            "reyher_part_no", "hopefix_part_no", "fastbolt_part_no",
            "schaefer_part_no", "kingb2b_part_no", "wasishop_part_no",
        ]

        def _build_sb_rows(raw_rows: list[dict]) -> list[dict]:
            result = []
            for r in raw_rows:
                part_no = _clean_mapping_val(r.get("gepcoop_part_no", ""))
                if not part_no:
                    continue
                result.append({col: _clean_mapping_val(r.get(col, "")) for col in DB_COLS})
            return result

        sb_rows = await asyncio.to_thread(_build_sb_rows, rows)

        def _do_upsert(sb_rows: list[dict]) -> int:
            # Full replace: delete all then batch upsert
            sb.table(TABLE).delete().neq("gepcoop_part_no", "").execute()
            total = len(sb_rows)
            batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            for i in range(batches):
                batch = sb_rows[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
                sb.table(TABLE).upsert(batch, on_conflict="gepcoop_part_no").execute()
                log.info(f"Supabase upsert batch {i+1}/{batches} ({len(batch)} rows)")
            return total

        supabase_rows = await asyncio.to_thread(_do_upsert, sb_rows)
        log.info(f"Supabase mapping frissítve: {supabase_rows} sor")
    else:
        log.warning("SUPABASE_URL/KEY hiányzik — csak lokális CSV mentve")

    log.info(f"Mapping frissítve: {len(rows)} sor, oszlopok={columns}, fájl={fname}")
    return {"filename": fname, "columns": columns, "rows": rows, "supabase_rows": supabase_rows}


@app.get("/admin/mapping-template")
def admin_mapping_template(
    authorization: str | None = Header(default=None),
):
    _get_admin(authorization)
    """Return an Excel (.xlsx) template with all column headers and two example rows."""
    import pandas as pd

    headers = [
        "gepcoop_part_no", "name",
        "csavarda_part_no", "irontrade_part_no", "koelner_part_no",
        "mekrs_part_no", "fabory_part_no", "ferdinand_part_no",
        "reyher_part_no", "hopefix_part_no", "fastbolt_part_no",
        "schaefer_part_no", "kingb2b_part_no", "wasishop_part_no",
    ]
    example_rows = [
        ["GC001", "Hatlapfejű csavar DIN 933 M8x20 horg.", "934012000000801000", "", "61025", "08555.18.02.100.100", "", "", "000094001000050112", "", "", "", "", ""],
        ["GC002", "Hatlapfejű csavar DIN 931 M10x50 horg.", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]
    df = pd.DataFrame(example_rows, columns=headers)
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
    for sid, creds in SUPPLIER_CREDS.items():
        meta = SUPPLIER_META.get(sid, {})
        entry = {
            "id":       sid,
            "url":      creds.get("url", ""),
            "username": creds.get("username", ""),
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
    _get_admin(authorization)
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
    log.info(f"Beszállítói adatok frissítve: {req.supplier_id}, username={req.username.strip()}")
    return {"supplier_id": req.supplier_id, "username": req.username.strip()}


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
