import asyncio
import csv
import io
import json
import logging
import os
import secrets
from fastapi import FastAPI, File, HTTPException, Header, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from pathlib import Path
from agent.tools import lookup_mapping_all, fetch_supplier_price, get_all_part_numbers, MAPPING_FILE

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
    "csavarda":  {"url": "https://csavarda.hu/",         "env": "SUPPLIER_A"},
    "irontrade": {"url": "https://irontrade.hu/",         "env": "SUPPLIER_B"},
    "koelner":   {"url": "https://webshop.koelner.hu/",   "env": "SUPPLIER_C"},
    "mekrs":     {"url": "https://eshop.mekrs.cz/en",     "env": "SUPPLIER_D"},
}

def _load_supplier_creds_from_env() -> dict:
    result = {}
    for sid, meta in SUPPLIER_META.items():
        env = meta["env"]
        result[sid] = {
            "url":      os.environ.get(f"{env}_URL", meta["url"]),
            "username": os.environ.get(f"{env}_USERNAME", ""),
            "password": os.environ.get(f"{env}_PASSWORD", ""),
        }
    return result

def _apply_suppliers_to_env(suppliers: dict) -> None:
    """Push credentials into os.environ so browser scripts pick them up."""
    for sid, creds in suppliers.items():
        env = SUPPLIER_META.get(sid, {}).get("env")
        if env:
            os.environ[f"{env}_URL"]      = creds.get("url", "")
            os.environ[f"{env}_USERNAME"] = creds.get("username", "")
            os.environ[f"{env}_PASSWORD"] = creds.get("password", "")

SUPPLIER_CREDS: dict = _load_supplier_creds_from_env()

# Admin credentials
_admin_password = os.environ.get("ADMIN_PASSWORD", "")
if not _admin_password:
    raise RuntimeError("ADMIN_PASSWORD must be set in .env")

ADMIN_USERS = {"admin": _admin_password}
admin_sessions: dict[str, str] = {}

UI_FILE = Path(__file__).parent / "ui" / "index.html"


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


# ── Recommendation logic ─────────────────────────────────────────────
def compute_recommendation(supplier_results: dict) -> dict:
    """
    Compare supplier results and return a purchase recommendation.
    Only HUF suppliers are ranked. CZK suppliers (e.g. mekrs) are shown
    as a reference note with their ECB-converted indicative price.
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

    # Only HUF suppliers enter the price ranking
    comparable = {sid: r for sid, r in available.items() if r.get("currency", "HUF") == "HUF"}
    cross_currency = {sid: r for sid, r in available.items() if r.get("currency", "HUF") != "HUF"}

    if not comparable:
        comparable = available
        cross_currency = {}

    if len(comparable) == 1 and not cross_currency:
        sid = next(iter(comparable))
        r = comparable[sid]
        stock_total = _total_stock(r.get("stock", 0))
        return {
            "winner": sid,
            "reason": (
                f"Csak a(z) {sid.capitalize()} adott vissza HUF árat. "
                f"Ár: {r['price_per_db']:.4f} HUF/db — "
                f"Készlet: {stock_total:,} db."
            ),
            "single_supplier": True,
        }

    prices  = {sid: r["price_per_db"] for sid, r in comparable.items()}
    winner  = min(prices, key=prices.get)
    others  = [s for s in prices if s != winner]
    loser   = others[0] if others else None

    winner_price = prices[winner]
    loser_price  = prices[loser] if loser else winner_price
    price_diff   = round(loser_price - winner_price, 6)
    savings_pct  = round((price_diff / loser_price) * 100, 1) if loser_price > 0 else 0.0

    winner_stock = _total_stock(comparable[winner].get("stock", 0))
    loser_stock  = _total_stock(comparable[loser].get("stock", 0)) if loser else 0

    stock_note = ""
    if loser and loser_stock > winner_stock * 2:
        stock_note = (
            f" Megjegyzés: a(z) {loser.capitalize()} lényegesen nagyobb készlettel rendelkezik "
            f"({loser_stock:,} vs {winner_stock:,} db) — érdemes mérlegelni az elérhetőséget."
        )

    cross_note = ""
    if cross_currency:
        parts = []
        for sid, r in cross_currency.items():
            huf      = r.get("price_per_db_huf")
            czk_rate = r.get("czk_huf_rate")
            if huf is not None and czk_rate is not None:
                parts.append(
                    f"{sid.capitalize()}: {r['price_per_db']:.4f} CZK/db"
                    f" ≈ {huf:.4f} HUF/db (1 CZK = {czk_rate} HUF, ECB)"
                )
            else:
                parts.append(f"{sid.capitalize()} ({r.get('currency','?')} — nem konvertált)")
        cross_note = f" ({'; '.join(parts)} — tájékoztató jellegű, nem szerepel a rangsorban.)"

    reason = (
        f"Vásárolj a(z) {winner.capitalize()}-tól — "
        f"{winner_price:.4f} HUF/db vs {loser_price:.4f} HUF/db "
        f"({savings_pct:.1f}%-kal olcsóbb, darabonként {price_diff:.4f} HUF megtakarítás)."
        f"{stock_note}{cross_note}"
    )

    # prices dict: only the HUF suppliers that were actually ranked
    all_prices = {sid: round(r["price_per_db"], 6) for sid, r in comparable.items()}
    all_stocks = {sid: _total_stock(r.get("stock", 0)) for sid, r in available.items()}

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


# ── Routes ────────────────────────────────────────────────────────────
@app.get("/")
def serve_ui():
    return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})


@app.post("/login")
def login(req: LoginRequest):
    if VALID_USERS.get(req.username) != req.password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = secrets.token_hex(32)
    sessions[token] = req.username
    return {"token": token, "username": req.username}


@app.get("/query/stream")
async def query_stream(
    internal_part_no: str,
    authorization: str | None = Header(default=None),
):
    _get_username(authorization)

    queue: asyncio.Queue = asyncio.Queue()

    async def runner():
        part = internal_part_no.strip()
        try:
            # ── Step 1: mapping lookup ───────────────────────────────
            await queue.put(("progress", {
                "step": "mapping", "status": "running",
                "msg": f"Looking up '{part}' in mapping table…",
            }))

            suppliers = lookup_mapping_all(part)
            log.info(f"Mapping result for '{part}': {suppliers}")

            if not suppliers:
                msg = f"Part number '{part}' was not found in the supplier mapping."
                await queue.put(("progress", {"step": "mapping", "status": "error", "msg": msg}))
                await queue.put(("error", {"message": msg}))
                return

            supplier_labels = ", ".join(s["supplier_id"] for s in suppliers)
            await queue.put(("progress", {
                "step": "mapping", "status": "done",
                "msg": f"Found {len(suppliers)} supplier(s): {supplier_labels}",
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
                    log.info(f"[{sid}] fetch done: price_per_db={r.get('price_per_db')}")
                except Exception as exc:
                    log.error(f"[{sid}] fetch failed: {exc}")
                    results[sid] = {"error": str(exc), "supplier_id": sid}
                    await queue.put(("progress", {
                        "step": "browser", "status": "error",
                        "msg": str(exc), "supplier": sid,
                    }))

            await asyncio.gather(*[fetch_one(s) for s in suppliers])

            # ── Step 3: recommendation ───────────────────────────────
            recommendation = compute_recommendation(results)
            log.info(f"Recommendation: {recommendation}")

            await queue.put(("result", {
                "internal_part_no": part,
                "suppliers":        results,
                "recommendation":   recommendation,
            }))

        except Exception as exc:
            log.exception(f"Unexpected error in runner: {exc}")
            await queue.put(("error", {"message": str(exc)}))
        finally:
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
    try:
        with open(MAPPING_FILE, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = list(reader.fieldnames or [])
            rows    = [dict(r) for r in reader]
    except FileNotFoundError:
        columns, rows = [], []
    return {"columns": columns, "rows": rows}


@app.post("/admin/upload-mapping")
async def admin_upload_mapping(
    file: UploadFile = File(...),
):
    content = await file.read()
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
        raise HTTPException(status_code=400, detail="A CSV fájl üres.")

    with open(MAPPING_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    log.info(f"Mapping frissítve: {len(rows)} sor, oszlopok={columns}, fájl={file.filename}")
    return {"filename": file.filename, "columns": columns, "rows": rows}


@app.delete("/admin/mapping")
def admin_delete_mapping():
    columns = []
    try:
        with open(MAPPING_FILE, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = list(reader.fieldnames or [])
    except FileNotFoundError:
        columns = ["gepcoop_part_no", "csavarda_part_no", "irontrade_part_no", "koelner_part_no", "mekrs_part_no"]

    with open(MAPPING_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)

    log.info(f"Mapping adatok törölve. Fejléc megmaradt: {columns}")
    return {"deleted": True, "columns": columns}


@app.get("/admin/suppliers")
def admin_get_suppliers():
    return {
        "suppliers": [
            {"id": sid, "url": creds.get("url", ""), "username": creds.get("username", ""), "password": creds.get("password", "")}
            for sid, creds in SUPPLIER_CREDS.items()
        ]
    }


@app.post("/admin/update-supplier")
def admin_update_supplier(
    req: UpdateSupplierRequest,
):
    if req.supplier_id not in SUPPLIER_CREDS:
        raise HTTPException(status_code=400, detail=f"Ismeretlen beszállító: {req.supplier_id}")
    if not req.username.strip() or not req.password:
        raise HTTPException(status_code=400, detail="Felhasználónév és jelszó megadása kötelező.")
    SUPPLIER_CREDS[req.supplier_id]["username"] = req.username.strip()
    SUPPLIER_CREDS[req.supplier_id]["password"] = req.password
    env_prefix = SUPPLIER_META[req.supplier_id]["env"]
    _update_env_file({
        f"{env_prefix}_USERNAME": req.username.strip(),
        f"{env_prefix}_PASSWORD": req.password,
    })
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
