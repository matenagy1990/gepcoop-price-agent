"""
Batch Price Agent – FastAPI backend
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _BUDAPEST = ZoneInfo("Europe/Budapest")
except Exception:  # pragma: no cover — tzdata hiányában esik vissza UTC-re
    _BUDAPEST = timezone.utc

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# GepcoopPriceAgent elérhetővé tétele importáláshoz
_AGENT_PATH = os.path.abspath(
    os.environ.get(
        "PRICE_AGENT_PATH",
        os.path.join(os.path.dirname(__file__), ".."),
    )
)
if _AGENT_PATH not in sys.path:
    sys.path.append(_AGENT_PATH)

from shared.supabase_client import get_supabase
from shared.supplier_registry import list_suppliers, get_supplier_name, SUPPLIERS
from batch.planner import build_preview
from batch.runner import run_batch
from batch.aggregator import build_matrix
from batch.export_excel import generate_excel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

BATCH_MAX_ITEMS = int(os.environ.get("BATCH_MAX_ITEMS", "50"))

app = FastAPI(title="Batch Price Agent", version="1.0.0")

# SSE futások: batch_run_id → asyncio.Queue
_active_runs: dict[str, asyncio.Queue] = {}

# In-memory eredmény cache – Supabase mentési hiba esetén is visszaadható
_completed_matrices: dict[str, dict] = {}   # run_id → {run_meta, matrix}

# ──────────────────────────────────────────────────────────────────────────────
# Pydantic modellek
# ──────────────────────────────────────────────────────────────────────────────

class PreviewRequest(BaseModel):
    project_name: str
    selected_suppliers: list[str]
    gepcoop_part_numbers: list[str]


class RunRequest(BaseModel):
    project_name: str
    selected_suppliers: list[str]
    gepcoop_part_numbers: list[str]


class ScheduleRequest(BaseModel):
    project_name: str
    selected_suppliers: list[str]
    gepcoop_part_numbers: list[str]
    # Manuális (lokál) módban kötelező; szerver (auto) módban figyelmen kívül
    # marad, mert a backend osztja ki a következő szabad 40 perces sávot.
    scheduled_at: str | None = None


class VipaCompleteLoginRequest(BaseModel):
    token: str


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_request(req_suppliers: list[str], req_parts: list[str]):
    if not req_suppliers:
        raise HTTPException(400, "Legalább egy webshopot ki kell választani.")
    if not req_parts:
        raise HTTPException(400, "Legalább egy cikkszámot meg kell adni.")
    if len(req_parts) > BATCH_MAX_ITEMS:
        raise HTTPException(400, f"Maximum {BATCH_MAX_ITEMS} cikkszám adható meg egyszerre.")
    unknown = [s for s in req_suppliers if s not in SUPPLIERS]
    if unknown:
        raise HTTPException(400, f"Ismeretlen webshop azonosítók: {unknown}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _save_batch_run(
    run_id: str,
    project_name: str,
    preview: dict,
    status: str = "running",
    scheduled_at: str | None = None,
) -> None:
    """Elmenti a futás fejlécét és a cikkszámait. `status="scheduled"` +
    `scheduled_at` esetén ütemezett (még nem futó) bejegyzést hoz létre."""
    sb = get_supabase()
    try:
        row = {
            "id": run_id,
            "project_name": project_name,
            "status": status,
            "selected_suppliers": preview["selected_suppliers"],
            "total_input_count": preview["total_items"],
            "unique_part_count": preview["total_items"],
            "searchable_count": preview["searchable_count"],
            "missing_mapping_count": preview["missing_mapping_count"],
            "created_at": _now_iso(),
        }
        if scheduled_at:
            row["scheduled_at"] = scheduled_at
        sb.table("batch_runs").insert(row).execute()

        items_payload = []
        for i, item in enumerate(preview["items"]):
            item_id = str(uuid.uuid4())
            item["db_id"] = item_id
            items_payload.append({
                "id": item_id,
                "batch_run_id": run_id,
                "row_index": i,
                "gepcoop_part_no": item["gepcoop_part_no"],
                "product_name": item.get("product_name"),
                "status": item.get("status", "pending"),
                "created_at": _now_iso(),
            })

        if items_payload:
            sb.table("batch_run_items").insert(items_payload).execute()

    except Exception as exc:
        log.error(f"Supabase mentési hiba: {exc}")


async def _save_results(run_id: str, results: list[dict], matrix: dict) -> None:
    sb = get_supabase()
    try:
        rows = []
        for r in results:
            rows.append({
                "id": str(uuid.uuid4()),
                "batch_run_id": run_id,
                "batch_run_item_id": r.get("batch_run_item_id"),
                "gepcoop_part_no": r["gepcoop_part_no"],
                "supplier_id": r["supplier_id"],
                "supplier_part_no": r.get("supplier_part_no"),
                "mapping_status": r.get("mapping_status", "mapped"),
                "scrape_status": r.get("scrape_status"),
                "raw_price": r.get("raw_price"),
                "currency": r.get("currency"),
                "unit": r.get("unit"),
                "normalized_price_huf": r.get("normalized_price_huf"),
                "stock_raw": r.get("stock_raw"),
                "stock_quantity": r.get("stock_quantity"),
                "is_top": False,
                "is_alternative": False,
                "error_message": r.get("error_message"),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "duration_ms": r.get("duration_ms"),
            })

        # TOP/ALT beállítás a mátrix alapján
        top_map: dict[tuple[str, str], str] = {}
        for item in matrix["items"]:
            pn = item["gepcoop_part_no"]
            for sid, cell in item["suppliers"].items():
                if cell.get("is_top"):
                    top_map[(pn, sid)] = "top"
                elif cell.get("is_alternative"):
                    top_map[(pn, sid)] = "alt"

        for row in rows:
            flag = top_map.get((row["gepcoop_part_no"], row["supplier_id"]))
            row["is_top"] = flag == "top"
            row["is_alternative"] = flag == "alt"

        if rows:
            sb.table("batch_run_supplier_results").insert(rows).execute()

        ok_count = sum(1 for r in results if r.get("scrape_status") == "ok")
        err_count = sum(1 for r in results if r.get("scrape_status") not in ("ok",))
        sb.table("batch_runs").update({
            "status": "completed" if ok_count > 0 else "failed",
            "success_count": ok_count,
            "error_count": err_count,
            "completed_at": _now_iso(),
        }).eq("id", run_id).execute()

    except Exception as exc:
        log.error(f"Eredmény mentési hiba: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/suppliers")
async def get_suppliers():
    return list_suppliers()


# ──────────────────────────────────────────────────────────────────────────────
# Vipa OTP belépés — ugyanaz a bevált flow, mint a fő price agentben.
# A session a megosztott assets/sessions/vipa_session.json fájlba kerül, így a
# scraper (browser/supplier_vipa.py) automatikusan újrahasználja ~20 órán át.
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/vipa/status")
async def vipa_status():
    """Van-e használható (mentett és friss) Vipa session?

    Szándékosan a könnyű, nem-destruktív ellenőrzést használja: nem indít
    böngészőt és nem törli a sessiont. Így a popup csak akkor jön fel, ha
    tényleg nincs friss session — egy flaky élő-teszt nem törölheti ki a jó
    sessiont és nem kérhet feleslegesen újra belépést.
    """
    from browser.vipa_otp import vipa_session_available
    return {"live": vipa_session_available()}


@app.post("/vipa/initiate-login")
async def vipa_initiate_login():
    """Headless OTP folyamat indítása: e-mail beírása + 'Log in', ami elküldi a tokent."""
    from browser.vipa_otp import start_vipa_otp_flow
    return start_vipa_otp_flow()


@app.post("/vipa/complete-login")
async def vipa_complete_login(req: VipaCompleteLoginRequest):
    """Az e-mailben kapott OTP token beküldése, a Vipa bejelentkezés véglegesítése."""
    from browser.vipa_otp import vipa_login_state
    if not vipa_login_state["active"]:
        raise HTTPException(
            status_code=400,
            detail="Nincs aktív Vipa OTP folyamat. Először kattintson az 'OTP kérése' gombra.",
        )
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="A token nem lehet üres.")

    vipa_login_state["token"] = token
    vipa_login_state["token_event"].set()

    try:
        await asyncio.wait_for(vipa_login_state["done_event"].wait(), timeout=90)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "A bejelentkezési folyamat időtúllépés miatt nem fejeződött be."}

    if vipa_login_state["error"]:
        raise HTTPException(status_code=400, detail=vipa_login_state["error"])

    return {"ok": True, "message": "Vipa bejelentkezés sikeres! A session el lett mentve."}


@app.post("/batch/preview")
async def batch_preview(req: PreviewRequest):
    _validate_request(req.selected_suppliers, req.gepcoop_part_numbers)
    try:
        preview = build_preview(
            project_name=req.project_name,
            selected_suppliers=req.selected_suppliers,
            gepcoop_part_numbers=req.gepcoop_part_numbers,
        )
        return preview
    except Exception as exc:
        log.exception("Preview hiba")
        raise HTTPException(500, str(exc))


async def _execute_batch(run_id: str, preview: dict, project_name: str, event_queue: asyncio.Queue) -> None:
    """A tényleges batch futtatás: scrapelés, mátrix, mentés. Azonnali és
    ütemezett futás is ezt hívja. Az event_queue-n keresztül streameli az
    eseményeket (az azonnali futásnál a UI ezt figyeli; ütemezettnél nincs néző)."""
    try:
        results = await run_batch(run_id, preview, event_queue)
        matrix = build_matrix(preview["items"], results, preview["selected_suppliers"])
        # Cache-eljük memóriában, hogy Supabase hiba esetén is visszaadjuk
        _completed_matrices[run_id] = {
            "run": {
                "id": run_id,
                "project_name": project_name,
                "status": "completed",
                "selected_suppliers": preview["selected_suppliers"],
                "unique_part_count": preview["total_items"],
                "created_at": _now_iso(),
            },
            "matrix": matrix,
        }
        await _save_results(run_id, results, matrix)
        await event_queue.put({"event": "done", "batch_run_id": run_id})
    except Exception as exc:
        log.exception("Batch futási hiba")
        await event_queue.put({"event": "error", "message": str(exc)})
    finally:
        # Queue nyitva marad, a stream kiolvasásig
        await asyncio.sleep(5)
        _active_runs.pop(run_id, None)


@app.post("/batch/run")
async def batch_run_start(req: RunRequest, background_tasks: BackgroundTasks):
    _validate_request(req.selected_suppliers, req.gepcoop_part_numbers)

    run_id = str(uuid.uuid4())
    event_queue: asyncio.Queue = asyncio.Queue()
    _active_runs[run_id] = event_queue

    try:
        preview = build_preview(
            project_name=req.project_name,
            selected_suppliers=req.selected_suppliers,
            gepcoop_part_numbers=req.gepcoop_part_numbers,
        )
    except Exception as exc:
        del _active_runs[run_id]
        raise HTTPException(500, str(exc))

    # Mentés Supabase-be (aszinkron)
    await _save_batch_run(run_id, req.project_name, preview)

    # Batch futás háttérben — a közös végrehajtó (azonnali és ütemezett is ezt használja)
    background_tasks.add_task(_execute_batch, run_id, preview, req.project_name, event_queue)

    return {"batch_run_id": run_id, "status": "started"}


# ── Ütemezési mód: lokál = manuális választás, Hetzner (Docker) = automatikus ──
AUTO_START_HOUR   = 20    # az auto-lánc 20:00-tól indul (budapesti idő)
AUTO_SLOT_MINUTES = 40    # szerveren minden futásra 40 perc jut
AUTO_END_HOUR     = 5     # az éjszaka vége a következő nap ~05:00
AUTO_HORIZON_DAYS = 7     # max ennyi napra előre keresünk szabad sávot


def _is_server_mode() -> bool:
    """True, ha a szerveren (Hetzner, Docker) futunk → automatikus ütemezés.
    A Docker konténer létrehozza a /.dockerenv fájlt; lokálisan (közvetlen
    uvicorn) ez nincs → manuális mód. A SCHEDULE_MODE env változóval felülírható."""
    override = os.environ.get("SCHEDULE_MODE", "").strip().lower()
    if override in ("auto", "server", "hetzner", "production"):
        return True
    if override in ("manual", "local"):
        return False
    return os.path.exists("/.dockerenv")


def _parse_scheduled_at(raw: str) -> datetime:
    """ISO időpont (UTC) értelmezése és validálása MANUÁLIS ütemezéshez.
    Tetszőleges perc megengedett — egyetlen elvárás, hogy jövőbeli legyen."""
    cleaned = (raw or "").strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(cleaned)
    except ValueError:
        raise HTTPException(400, "Érvénytelen időpont formátum.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc).replace(microsecond=0)
    if when <= datetime.now(timezone.utc):
        raise HTTPException(400, "Az ütemezett időpont a múltban van.")
    return when


def _taken_scheduled_utc(sb) -> set:
    """A már lefoglalt ütemezett időpontok halmaza (UTC, másodpercre kerekítve)."""
    rows = (
        sb.table("batch_runs").select("scheduled_at")
        .eq("status", "scheduled").execute().data
    ) or []
    taken = set()
    for r in rows:
        sa = r.get("scheduled_at")
        if not sa:
            continue
        try:
            dt = datetime.fromisoformat(str(sa).replace("Z", "+00:00")).astimezone(timezone.utc)
            taken.add(dt.replace(microsecond=0))
        except Exception:
            pass
    return taken


def _next_auto_slot(sb) -> datetime | None:
    """A következő SZABAD 40 perces sáv 20:00-tól (budapesti idő), éjszakánként
    ~05:00-ig, max AUTO_HORIZON_DAYS napra előre. UTC datetime-ot ad vissza,
    vagy None, ha minden tele van. Így ha valaki ütemez → 20:00, a következő →
    20:40, aztán 21:20… (a már foglalt sávokat kihagyja)."""
    now_utc = datetime.now(timezone.utc)
    taken = _taken_scheduled_utc(sb)
    today_local = datetime.now(_BUDAPEST).date()
    for day in range(AUTO_HORIZON_DAYS):
        d = today_local + timedelta(days=day)
        base = datetime(d.year, d.month, d.day, AUTO_START_HOUR, 0, tzinfo=_BUDAPEST)
        night_end = datetime(d.year, d.month, d.day, AUTO_END_HOUR, 0, tzinfo=_BUDAPEST) + timedelta(days=1)
        slot = base
        while slot < night_end:
            slot_utc = slot.astimezone(timezone.utc).replace(microsecond=0)
            if slot_utc > now_utc and slot_utc not in taken:
                return slot_utc
            slot += timedelta(minutes=AUTO_SLOT_MINUTES)
    return None


@app.get("/config")
async def get_config():
    """A felület ez alapján dönti el: manuális sáv-választó (lokál) vagy
    automatikus 20:00-os ütemezés (szerver)."""
    server = _is_server_mode()
    return {
        "schedule_mode": "auto" if server else "manual",
        "auto_spacing_min": AUTO_SLOT_MINUTES,
        "auto_start_hour": AUTO_START_HOUR,
    }


@app.post("/batch/schedule")
async def batch_schedule(req: ScheduleRequest):
    """Ütemezett futás létrehozása. Nem indít scrapert — eltárolja a futást
    `scheduled` státusszal, a háttér-ütemező indítja majd az időpontban.

    Szerver (auto) módban a backend osztja ki a következő szabad 40 perces sávot
    20:00-tól; lokál (manuális) módban a megadott időpontot használja."""
    _validate_request(req.selected_suppliers, req.gepcoop_part_numbers)
    sb = get_supabase()

    try:
        if _is_server_mode():
            when = _next_auto_slot(sb)
            if when is None:
                raise HTTPException(409, "Nincs szabad ütemezési sáv a következő napokban.")
            when_iso = when.isoformat(timespec="seconds")
        else:
            if not (req.scheduled_at or "").strip():
                raise HTTPException(400, "Adj meg egy időpontot az ütemezéshez.")
            when = _parse_scheduled_at(req.scheduled_at)
            when_iso = when.isoformat(timespec="seconds")
            existing = (
                sb.table("batch_runs").select("id")
                .eq("status", "scheduled").eq("scheduled_at", when_iso).execute().data
            )
            if existing:
                raise HTTPException(409, "Ez az időpont már foglalt — válassz másik sávot.")
    except HTTPException:
        raise
    except Exception as exc:
        # Tipikusan akkor, ha a 'scheduled_at' oszlop még nincs felvéve a táblába.
        raise HTTPException(500, f"Ütemezési hiba (van scheduled_at oszlop?): {exc}")

    run_id = str(uuid.uuid4())
    try:
        preview = build_preview(
            project_name=req.project_name,
            selected_suppliers=req.selected_suppliers,
            gepcoop_part_numbers=req.gepcoop_part_numbers,
        )
    except Exception as exc:
        raise HTTPException(500, str(exc))

    await _save_batch_run(run_id, req.project_name, preview, status="scheduled", scheduled_at=when_iso)
    return {
        "batch_run_id": run_id,
        "status": "scheduled",
        "scheduled_at": when_iso,
        "mode": "auto" if _is_server_mode() else "manual",
    }


@app.get("/batch/run/{run_id}/progress")
async def batch_progress_stream(run_id: str):
    """SSE stream a futás eseményeihez."""
    queue = _active_runs.get(run_id)
    if queue is None:
        raise HTTPException(404, f"Nincs aktív futás: {run_id}")

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("event") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield "data: {\"event\": \"heartbeat\"}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/batch/run/{run_id}")
async def get_batch_run(run_id: str):
    # Elsőként az in-memory cache-t ellenőrizzük (Supabase mentési hiba esetén is működik)
    if run_id in _completed_matrices:
        return _completed_matrices[run_id]

    sb = get_supabase()
    try:
        run_res = sb.table("batch_runs").select("*").eq("id", run_id).limit(1).execute()
        if not run_res.data:
            raise HTTPException(404, f"Futás nem található: {run_id}")

        items_res = sb.table("batch_run_items").select("*").eq("batch_run_id", run_id).order("row_index").execute()
        results_res = sb.table("batch_run_supplier_results").select("*").eq("batch_run_id", run_id).execute()

        run = run_res.data[0]
        items = items_res.data or []
        raw_results = results_res.data or []

        # Mátrix rekonstruálása
        preview_items = []
        for item in items:
            pn = item["gepcoop_part_no"]
            supplier_map = {}
            for sid in run.get("selected_suppliers") or []:
                supplier_map[sid] = {"status": "missing_mapping", "supplier_part_no": None}
            for r in raw_results:
                if r["gepcoop_part_no"] == pn and r["mapping_status"] == "mapped":
                    sid = r["supplier_id"]
                    supplier_map[sid] = {
                        "status": "mapped",
                        "supplier_part_no": r.get("supplier_part_no"),
                    }
            preview_items.append({
                "gepcoop_part_no": pn,
                "product_name": item.get("product_name"),
                "suppliers": supplier_map,
            })

        matrix = build_matrix(preview_items, raw_results, run.get("selected_suppliers") or [])

        return {
            "run": run,
            "matrix": matrix,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Futás lekérési hiba")
        raise HTTPException(500, str(exc))


@app.get("/batch/runs")
async def list_batch_runs(limit: int = 50):
    sb = get_supabase()
    try:
        res = sb.table("batch_runs").select("*").order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/batch/run/{run_id}")
async def delete_run(run_id: str):
    """Futás törlése.
    - Ütemezett (scheduled) → eltűnik, így este NEM fog elindulni (a sávja
      újra szabaddá válik).
    - Lefutott (completed/failed) → a teljes eredményével együtt törlődik.
    - Éppen futó (running) → NEM törölhető (várjuk meg, amíg befejeződik).
    """
    sb = get_supabase()
    run = sb.table("batch_runs").select("status").eq("id", run_id).limit(1).execute().data
    if not run:
        raise HTTPException(404, "Futás nem található.")
    if run[0].get("status") == "running":
        raise HTTPException(400, "Éppen futó lekérdezés nem törölhető — várd meg, amíg befejeződik.")
    try:
        # Az eredmények és cikkszámok is törlődnek (ütemezettnél eredmény még nincs).
        sb.table("batch_run_supplier_results").delete().eq("batch_run_id", run_id).execute()
        sb.table("batch_run_items").delete().eq("batch_run_id", run_id).execute()
        sb.table("batch_runs").delete().eq("id", run_id).execute()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    # A memóriás eredmény-cache-ből is kivesszük, ha ott van.
    _completed_matrices.pop(run_id, None)
    return {"status": "deleted", "id": run_id}


@app.get("/batch/run/{run_id}/export.xlsx")
async def export_excel(run_id: str):
    sb = get_supabase()
    try:
        run_res = sb.table("batch_runs").select("*").eq("id", run_id).limit(1).execute()
        if not run_res.data:
            raise HTTPException(404, f"Futás nem található: {run_id}")

        items_res = sb.table("batch_run_items").select("*").eq("batch_run_id", run_id).order("row_index").execute()
        results_res = sb.table("batch_run_supplier_results").select("*").eq("batch_run_id", run_id).execute()

        run = run_res.data[0]
        items = items_res.data or []
        raw_results = results_res.data or []

        preview_items = []
        for item in items:
            pn = item["gepcoop_part_no"]
            supplier_map = {}
            for sid in run.get("selected_suppliers") or []:
                supplier_map[sid] = {"status": "missing_mapping", "supplier_part_no": None}
            for r in raw_results:
                if r["gepcoop_part_no"] == pn and r["mapping_status"] == "mapped":
                    sid = r["supplier_id"]
                    supplier_map[sid] = {
                        "status": "mapped",
                        "supplier_part_no": r.get("supplier_part_no"),
                    }
            preview_items.append({
                "gepcoop_part_no": pn,
                "product_name": item.get("product_name"),
                "suppliers": supplier_map,
            })

        matrix = build_matrix(preview_items, raw_results, run.get("selected_suppliers") or [])

        run_meta = {
            "project_name": run.get("project_name", ""),
            "batch_run_id": run_id,
            "created_at": run.get("created_at", ""),
            "status": run.get("status", ""),
        }

        xlsx_bytes = generate_excel(run_meta, matrix)

        safe_name = "".join(c if (c.isascii() and (c.isalnum() or c in "- _")) else "_" for c in run.get("project_name", "batch"))
        filename = f"batch_{safe_name}_{run_id[:8]}.xlsx"

        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Export hiba")
        raise HTTPException(500, str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Háttér-ütemező: az esedékes ütemezett futásokat automatikusan elindítja
# ──────────────────────────────────────────────────────────────────────────────

async def _execute_scheduled(run: dict) -> None:
    """Egy ütemezett futás végrehajtása az időpontjában. A státuszt a hívó
    (scheduler loop) már 'running'-ra állította. A cikkszámokat a tárolt
    batch_run_items-ből töltjük vissza, és a mappinget frissen újraépítjük."""
    sb = get_supabase()
    run_id = run["id"]
    try:
        items = (
            sb.table("batch_run_items").select("id,gepcoop_part_no")
            .eq("batch_run_id", run_id).order("row_index").execute().data
        ) or []
        parts = [it["gepcoop_part_no"] for it in items if it.get("gepcoop_part_no")]
        id_by_part = {(it.get("gepcoop_part_no") or "").strip().upper(): it["id"] for it in items}

        preview = build_preview(
            project_name=run.get("project_name", ""),
            selected_suppliers=run.get("selected_suppliers") or [],
            gepcoop_part_numbers=parts,
        )
        # A frissen épített preview item-jeihez visszakötjük az eredeti DB sor-azonosítókat,
        # hogy az eredmények a már létező batch_run_items-hez kapcsolódjanak.
        for it in preview["items"]:
            it["db_id"] = id_by_part.get((it["gepcoop_part_no"] or "").strip().upper())

        event_queue: asyncio.Queue = asyncio.Queue()
        _active_runs[run_id] = event_queue
        await _execute_batch(run_id, preview, run.get("project_name", ""), event_queue)
    except Exception as exc:
        log.exception("Ütemezett futás hiba (%s): %s", run_id, exc)
        try:
            sb.table("batch_runs").update(
                {"status": "failed", "completed_at": _now_iso()}
            ).eq("id", run_id).execute()
        except Exception:
            pass


async def _scheduler_loop() -> None:
    """Percenként megnézi, van-e esedékes (scheduled_at <= most) ütemezett futás,
    és atomikusan 'running'-ra váltva elindítja. A lejárt (a szerver állása alatt
    elmaradt) futások a következő körben automatikusan elindulnak."""
    await asyncio.sleep(5)  # adjunk időt az app teljes indulásának
    while True:
        try:
            sb = get_supabase()
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            due = (
                sb.table("batch_runs").select("*")
                .eq("status", "scheduled").lte("scheduled_at", now_iso).execute().data
            ) or []
            for run in due:
                # Atomikus átállítás: csak az viszi tovább, aki 'scheduled'→'running'-ra
                # tudta váltani (véd a dupla indítás ellen).
                flipped = (
                    sb.table("batch_runs").update({"status": "running"})
                    .eq("id", run["id"]).eq("status", "scheduled").execute()
                )
                if flipped.data:
                    log.info("Ütemezett futás indul: %s (%s)", run["id"], run.get("scheduled_at"))
                    asyncio.create_task(_execute_scheduled(run))
        except Exception as exc:
            log.warning("Scheduler loop hiba (figyelmen kívül hagyva): %s", exc)
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_scheduler() -> None:
    asyncio.create_task(_scheduler_loop())
    log.info("Ütemező háttérfolyamat elindítva (60 mp-enként ellenőriz).")
