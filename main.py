import asyncio
import csv
import io
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from fastapi import FastAPI, File, HTTPException, Header, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from pathlib import Path
from agent.tools import lookup_mapping_all, fetch_supplier_price, get_all_part_numbers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

app = FastAPI(title="Gép-Coop Price Agent", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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

# ── Auth ─────────────────────────────────────────────────────────────
_app_username = os.environ.get("APP_USERNAME", "")
_app_password = os.environ.get("APP_PASSWORD", "")
if not _app_username or not _app_password:
    raise RuntimeError("APP_USERNAME and APP_PASSWORD must be set in .env")

VALID_USERS: dict = {_app_username: _app_password}
sessions: dict[str, str] = {}   # token → username

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

# Admin credentials
_admin_password = os.environ.get("ADMIN_PASSWORD", "")
if not _admin_password:
    raise RuntimeError("ADMIN_PASSWORD must be set in .env")

ADMIN_USERS = {"admin": _admin_password}
admin_sessions: dict[str, str] = {}

UI_FILE   = Path(__file__).parent / "ui" / "index.html"
LOGO_FILE = Path(__file__).parent / "assets" / "logo.png"


def _get_username(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    if token not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return sessions[token]


def _get_admin(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    if token not in admin_sessions:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    return admin_sessions[token]


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

class AdminLoginRequest(BaseModel):
    password: str

class UpdateUserRequest(BaseModel):
    username: str
    password: str

class UpdateSupplierRequest(BaseModel):
    supplier_id: str
    username: str
    password: str
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


@app.post("/login")
def login(req: LoginRequest):
    if VALID_USERS.get(req.username) != req.password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = secrets.token_hex(32)
    sessions[token] = req.username
    return {"token": token, "username": req.username}


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
                    # Schaefer rotates passwords monthly — prompt user to update
                    if sid == "schaefer" and "failed" in err_msg.lower() and "credential" in err_msg.lower():
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/supplier/update-password")
def update_supplier_password(
    req: UpdatePasswordRequest,
    authorization: str | None = Header(default=None),
):
    """Allow a logged-in user to update a supplier's password (e.g. after Schaefer monthly rotation)."""
    _get_username(authorization)
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

@app.post("/admin/login")
def admin_login(req: AdminLoginRequest):
    if ADMIN_USERS.get("admin") != req.password:
        raise HTTPException(status_code=401, detail="Hibás admin jelszó.")
    token = secrets.token_hex(32)
    admin_sessions[token] = "admin"
    return {"token": token, "username": "admin"}


@app.get("/admin/mapping")
def admin_get_mapping():
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
):
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
def admin_mapping_template():
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
def admin_delete_mapping():
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
def admin_get_runs():
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


@app.get("/admin/suppliers")
def admin_get_suppliers():
    result = []
    for sid, creds in SUPPLIER_CREDS.items():
        meta = SUPPLIER_META.get(sid, {})
        entry = {
            "id":       sid,
            "url":      creds.get("url", ""),
            "username": creds.get("username", ""),
            "password": creds.get("password", ""),
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
):
    if req.supplier_id not in SUPPLIER_CREDS:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {req.supplier_id}")
    if not req.username.strip() or not req.password:
        raise HTTPException(status_code=400, detail="Felhasználónév és jelszó megadása kötelező.")
    meta       = SUPPLIER_META[req.supplier_id]
    env_prefix = meta["env"]

    SUPPLIER_CREDS[req.supplier_id]["username"] = req.username.strip()
    SUPPLIER_CREDS[req.supplier_id]["password"] = req.password
    env_updates = {
        f"{env_prefix}_USERNAME": req.username.strip(),
        f"{env_prefix}_PASSWORD": req.password,
    }
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
def admin_get_users():
    return {"users": [{"username": u} for u in VALID_USERS.keys()]}


@app.post("/admin/update-user")
def admin_update_user(
    req: UpdateUserRequest,
):
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Felhasználónév és jelszó megadása kötelező.")
    VALID_USERS.clear()
    VALID_USERS[username] = req.password
    _update_env_file({"APP_USERNAME": username, "APP_PASSWORD": req.password})
    sessions.clear()   # invalidate all active sessions — re-login required
    log.info(f"Felhasználói adatok frissítve: username={username}")
    return {"username": username}
