"""
Batch scraper futtatás.
Supplierenként csoportosítja a feladatokat, párhuzamosan futtatja a workereket,
és SSE eseményeket küld a frontendnek.

Egy webshopon belül kétféle úton futhatunk:
  • Batch út (preferált): ha a scraper kínál `fetch_prices(parts, …)` belépőt,
    EGY böngészőt indít, egyszer lép be, és azon az egy session-ön végigkeresi
    az összes cikket. Kevesebb böngészőindítás, kevesebb login → gyorsabb és
    stabilabb.
  • Per-cikk út (visszaesés): ha a scrapernek még csak `fetch_price(part)`-ja
    van, cikkenként külön hívjuk (a korábbi viselkedés, változatlanul).
"""
import asyncio
import importlib
import logging
import os
import sys
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BATCH_SUPPLIER_LIMIT = int(os.environ.get("BATCH_SUPPLIER_LIMIT", "4"))
SCRAPER_TIMEOUT = int(os.environ.get("SCRAPER_TIMEOUT_SECONDS", "60"))
SCRAPER_RETRY = int(os.environ.get("SCRAPER_RETRY_COUNT", "1"))

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(BATCH_SUPPLIER_LIMIT)
    return _semaphore


def _import_price_agent():
    """Add GepcoopPriceAgent to sys.path so we can import agent.tools and browser.*"""
    agent_path = os.environ.get(
        "PRICE_AGENT_PATH",
        os.path.join(os.path.dirname(__file__), "..", ".."),
    )
    agent_path = os.path.abspath(agent_path)
    if agent_path not in sys.path:
        sys.path.append(agent_path)


def _get_batch_fetcher(supplier_id: str):
    """Return the scraper's `fetch_prices` callable if it offers one, else None.

    A scraper opts into the optimised batch path simply by defining
    `fetch_prices`; otherwise we transparently fall back to per-part `fetch_price`.
    """
    _import_price_agent()
    try:
        mod = importlib.import_module(f"browser.supplier_{supplier_id}")
    except Exception as exc:
        log.warning(f"{supplier_id}: scraper modul nem importálható: {exc}")
        return None
    return getattr(mod, "fetch_prices", None)


def _manual_huf_rate(currency: str):
    """Az agent.tools admin-konfigurált árfolyam-segédje (csak olvasás)."""
    _import_price_agent()
    try:
        from agent.tools import _get_manual_huf_rate
        return _get_manual_huf_rate(currency)
    except Exception:
        return None


def _normalize_raw(raw: dict) -> dict:
    """A nyers scraper-eredményt per-db árra normalizálja, EUR esetén HUF-ot is
    számol — ugyanaz a logika, mint az agent.tools.fetch_supplier_price-ben."""
    try:
        raw["price_per_db"] = round(raw["price_raw"] / raw["price_unit_qty"], 6)
    except Exception:
        raw["price_per_db"] = None
    currency = raw.get("currency", "HUF")
    if currency != "HUF" and raw.get("price_per_db") is not None:
        rate = _manual_huf_rate(currency)
        if rate is not None:
            raw["price_per_db_huf"] = round(raw["price_per_db"] * rate, 6)
            raw["fx_huf_rate"] = round(rate, 4)
    return raw


# ── Eredmény-sor építők (mindkét út ezeket használja) ───────────────────────────

def _ok_row(batch_run_id, task, supplier_id, raw, started_at, completed_at, duration_ms) -> dict:
    return {
        "batch_run_id": batch_run_id,
        "batch_run_item_id": task.get("item_id"),
        "gepcoop_part_no": task["gepcoop_part_no"],
        "supplier_id": supplier_id,
        "supplier_part_no": task["supplier_part_no"],
        "mapping_status": "mapped",
        "scrape_status": "ok",
        "raw_price": raw.get("price_raw"),
        "currency": raw.get("currency", "HUF"),
        "unit": raw.get("unit", "db"),
        "price_unit_qty": raw.get("price_unit_qty", 1),
        "normalized_price_huf": raw.get("price_per_db_huf") or raw.get("price_per_db"),
        "stock_raw": str(raw.get("stock", "")) if raw.get("stock") is not None else None,
        "stock_quantity": _parse_stock(raw.get("stock")),
        "product_url": raw.get("product_url"),
        "error_message": None,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": duration_ms,
        "is_manual": raw.get("manual", False),
    }


def _err_row(batch_run_id, task, supplier_id, msg, started_at, completed_at, duration_ms) -> dict:
    return {
        "batch_run_id": batch_run_id,
        "batch_run_item_id": task.get("item_id"),
        "gepcoop_part_no": task["gepcoop_part_no"],
        "supplier_id": supplier_id,
        "supplier_part_no": task["supplier_part_no"],
        "mapping_status": "mapped",
        "scrape_status": _classify_error(msg),
        "raw_price": None,
        "currency": None,
        "unit": None,
        "price_unit_qty": None,
        "normalized_price_huf": None,
        "stock_raw": None,
        "stock_quantity": None,
        "error_message": msg,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": duration_ms,
        "is_manual": False,
    }


async def _fetch_with_retry(supplier_id: str, supplier_part_no: str, on_progress) -> dict:
    _import_price_agent()
    from agent.tools import fetch_supplier_price

    last_exc = None
    for attempt in range(1 + SCRAPER_RETRY):
        try:
            result = await asyncio.wait_for(
                fetch_supplier_price(supplier_id, supplier_part_no, on_progress),
                timeout=SCRAPER_TIMEOUT,
            )
            return result
        except asyncio.TimeoutError:
            last_exc = TimeoutError(f"Időtúllépés ({SCRAPER_TIMEOUT}s)")
            if attempt < SCRAPER_RETRY:
                log.warning(f"{supplier_id}/{supplier_part_no}: timeout, újra…")
                await asyncio.sleep(3)
        except RuntimeError as exc:
            # NOT_FOUND / NOT_PRICED — nem érdemes retry-olni
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < SCRAPER_RETRY:
                log.warning(f"{supplier_id}/{supplier_part_no}: hiba, újra… ({exc})")
                await asyncio.sleep(3)
    raise last_exc or RuntimeError("Ismeretlen hiba")


async def run_supplier_worker(
    batch_run_id: str,
    supplier_id: str,
    tasks: list[dict],          # [{gepcoop_part_no, supplier_part_no, item_id}]
    event_queue: asyncio.Queue,
) -> list[dict]:
    """Egy supplier összes cikkszámát lekérdezi. Ha a scraper kínál batch
    belépőt (`fetch_prices`), azt használja (egy böngésző, egy login); egyébként
    a per-cikk útra esik vissza."""
    fetch_prices = _get_batch_fetcher(supplier_id)
    if fetch_prices is not None:
        return await _run_supplier_batched(batch_run_id, supplier_id, tasks, event_queue, fetch_prices)
    return await _run_supplier_sequential(batch_run_id, supplier_id, tasks, event_queue)


async def _run_supplier_batched(
    batch_run_id: str,
    supplier_id: str,
    tasks: list[dict],
    event_queue: asyncio.Queue,
    fetch_prices,
) -> list[dict]:
    """Batch út: a scraper `fetch_prices`-ával egy böngészőben kérdezi le az
    összes cikket."""
    await event_queue.put({
        "event": "supplier_started",
        "supplier_id": supplier_id,
        "total": len(tasks),
    })

    part_nos = [t["supplier_part_no"] for t in tasks]
    emitted: set[int] = set()
    started_at = datetime.now(timezone.utc)

    async def on_progress(data: dict):
        await event_queue.put({
            "event": "item_progress",
            "supplier_id": supplier_id,
            **data,
        })

    async def on_item(index: int, total: int, part_no: str, raw, error):
        emitted.add(index)
        gepcoop_pn = tasks[index]["gepcoop_part_no"] if index < len(tasks) else None
        if error is None:
            await event_queue.put({
                "event": "item_completed",
                "supplier_id": supplier_id,
                "gepcoop_part_no": gepcoop_pn,
                "index": index,
                "total": total,
                "normalized_price_huf": (raw or {}).get("price_per_db_huf") or (raw or {}).get("price_per_db"),
            })
        else:
            await event_queue.put({
                "event": "item_failed",
                "supplier_id": supplier_id,
                "gepcoop_part_no": gepcoop_pn,
                "error": error,
                "index": index,
                "total": total,
            })

    raw_list: list[dict] = []
    try:
        raw_list = await asyncio.wait_for(
            fetch_prices(part_nos, on_progress=on_progress, on_item=on_item),
            timeout=SCRAPER_TIMEOUT * max(len(tasks), 1),
        )
    except Exception as exc:
        # Egész webshopot érintő hiba (pl. login) — minden cikk hibás lesz.
        msg = str(exc) or repr(exc)
        if isinstance(exc, asyncio.TimeoutError):
            msg = f"Időtúllépés ({SCRAPER_TIMEOUT * max(len(tasks), 1)}s)"
        log.warning(f"{supplier_id}: batch fetch_prices hiba: {msg}")
        raw_list = [{"supplier_part_no": pn, "error": msg} for pn in part_nos]
        # Azokra a cikkekre, amikre az on_item még nem ment el, küldjünk failed-et.
        for i, pn in enumerate(part_nos):
            if i not in emitted:
                await event_queue.put({
                    "event": "item_failed",
                    "supplier_id": supplier_id,
                    "gepcoop_part_no": tasks[i]["gepcoop_part_no"],
                    "error": msg,
                    "index": i,
                    "total": len(part_nos),
                })

    completed_at = datetime.now(timezone.utc)
    total_ms = int((completed_at - started_at).total_seconds() * 1000)
    per_item_ms = total_ms // max(len(tasks), 1)

    # Eredménysorok összeállítása, a part_nos sorrendjéhez igazítva.
    results: list[dict] = []
    for i, task in enumerate(tasks):
        raw = raw_list[i] if i < len(raw_list) else {"error": "nincs eredmény"}
        if raw.get("error"):
            results.append(_err_row(batch_run_id, task, supplier_id, raw["error"],
                                    started_at, completed_at, per_item_ms))
        else:
            _normalize_raw(raw)
            results.append(_ok_row(batch_run_id, task, supplier_id, raw,
                                   started_at, completed_at, per_item_ms))

    await event_queue.put({
        "event": "supplier_completed",
        "supplier_id": supplier_id,
        "total": len(tasks),
        "ok": sum(1 for r in results if r["scrape_status"] == "ok"),
        "errors": sum(1 for r in results if r["scrape_status"] != "ok"),
    })
    return results


async def _run_supplier_sequential(
    batch_run_id: str,
    supplier_id: str,
    tasks: list[dict],
    event_queue: asyncio.Queue,
) -> list[dict]:
    """Per-cikk visszaesési út: cikkenként külön fetch_price (a korábbi
    viselkedés, változatlanul)."""
    results = []

    await event_queue.put({
        "event": "supplier_started",
        "supplier_id": supplier_id,
        "total": len(tasks),
    })

    async def on_progress(data: dict):
        await event_queue.put({
            "event": "item_progress",
            "supplier_id": supplier_id,
            **data,
        })

    for i, task in enumerate(tasks):
        gepcoop_pn = task["gepcoop_part_no"]
        supplier_pn = task["supplier_part_no"]

        await event_queue.put({
            "event": "item_started",
            "supplier_id": supplier_id,
            "gepcoop_part_no": gepcoop_pn,
            "supplier_part_no": supplier_pn,
            "index": i,
            "total": len(tasks),
        })

        started_at = datetime.now(timezone.utc)
        try:
            raw = await _fetch_with_retry(supplier_id, supplier_pn, on_progress)
            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            result = _ok_row(batch_run_id, task, supplier_id, raw, started_at, completed_at, duration_ms)

            await event_queue.put({
                "event": "item_completed",
                "supplier_id": supplier_id,
                "gepcoop_part_no": gepcoop_pn,
                "index": i,
                "total": len(tasks),
                "normalized_price_huf": result["normalized_price_huf"],
            })

        except RuntimeError as exc:
            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)
            msg = str(exc)
            result = _err_row(batch_run_id, task, supplier_id, msg, started_at, completed_at, duration_ms)
            await event_queue.put({
                "event": "item_failed",
                "supplier_id": supplier_id,
                "gepcoop_part_no": gepcoop_pn,
                "error": msg,
                "index": i,
                "total": len(tasks),
            })

        except Exception as exc:
            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)
            msg = str(exc)
            result = _err_row(batch_run_id, task, supplier_id, msg, started_at, completed_at, duration_ms)
            # _classify_error az _err_row-ban "error"-t ad ismeretlen üzenetre
            result["scrape_status"] = "error"
            await event_queue.put({
                "event": "item_failed",
                "supplier_id": supplier_id,
                "gepcoop_part_no": gepcoop_pn,
                "error": msg,
                "index": i,
                "total": len(tasks),
            })

        results.append(result)
        if i < len(tasks) - 1:
            await asyncio.sleep(1)

    await event_queue.put({
        "event": "supplier_completed",
        "supplier_id": supplier_id,
        "total": len(tasks),
        "ok": sum(1 for r in results if r["scrape_status"] == "ok"),
        "errors": sum(1 for r in results if r["scrape_status"] != "ok"),
    })

    return results


def _classify_error(msg: str) -> str:
    lower = msg.lower()
    if "nem találom" in lower or "not found" in lower:
        return "not_found"
    if "nincs beárazva" in lower or "not priced" in lower:
        return "not_priced"
    if "időtúllépés" in lower or "timeout" in lower:
        return "timeout"
    if "belépés" in lower or "login" in lower:
        return "login_failed"
    return "error"


def _parse_stock(stock_val) -> int | None:
    if stock_val is None:
        return None
    if isinstance(stock_val, int):
        return stock_val
    if isinstance(stock_val, dict):
        return sum(v for v in stock_val.values() if isinstance(v, (int, float)))
    try:
        import re
        digits = re.findall(r"\d+", str(stock_val))
        return int("".join(digits)) if digits else None
    except Exception:
        return None


async def run_batch(
    batch_run_id: str,
    preview: dict,
    event_queue: asyncio.Queue,
) -> list[dict]:
    """
    Fő batch futtatás.
    Supplierenként csoportosítja a feladatokat, max BATCH_SUPPLIER_LIMIT párhuzamos worker fut.
    Visszaadja az összes eredményt.
    """
    # Supplier → task lista felépítése
    supplier_tasks: dict[str, list[dict]] = {}
    for item in preview["items"]:
        pn = item["gepcoop_part_no"]
        item_id = item.get("db_id")
        for sid, sdata in item["suppliers"].items():
            if sdata["status"] == "mapped" and sdata["supplier_part_no"]:
                supplier_tasks.setdefault(sid, []).append({
                    "gepcoop_part_no": pn,
                    "supplier_part_no": sdata["supplier_part_no"],
                    "item_id": item_id,
                })

    await event_queue.put({
        "event": "batch_started",
        "batch_run_id": batch_run_id,
        "supplier_count": len(supplier_tasks),
        "total_searches": sum(len(v) for v in supplier_tasks.values()),
    })

    sem = _get_semaphore()
    all_results: list[dict] = []

    async def run_with_sem(sid: str, tasks: list[dict]):
        async with sem:
            return await run_supplier_worker(batch_run_id, sid, tasks, event_queue)

    worker_coros = [run_with_sem(sid, tasks) for sid, tasks in supplier_tasks.items()]
    results_per_supplier = await asyncio.gather(*worker_coros, return_exceptions=False)

    for supplier_results in results_per_supplier:
        if isinstance(supplier_results, list):
            all_results.extend(supplier_results)

    await event_queue.put({
        "event": "batch_completed",
        "batch_run_id": batch_run_id,
        "total": len(all_results),
        "ok": sum(1 for r in all_results if r["scrape_status"] == "ok"),
        "errors": sum(1 for r in all_results if r["scrape_status"] != "ok"),
    })

    return all_results
