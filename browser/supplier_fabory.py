"""
Playwright scraper for fabory.com (Supplier E)

Session flow:
  1. Try to restore saved session from assets/sessions/fabory_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to search URL — if redirected to /login → session invalid
  3. If session missing / stale / invalid: full login, save new session

Login flow (fallback):
  1. GET /hu/login → accept cookie banner → fill email + password → "Belépés"
  2. Redirects to /hu on success

Search flow:
  4. /hu/search?text={supplier_part_no}
  5. Click first product link
  6. Extract Nettó ár (price), Ár / (unit qty), Készlet (stock)

Price format: "605 Ft" → 605 HUF per unit_qty pieces (unit_qty from "Ár /" column)
Stock format: "Készleten" = in stock, anything else = out of stock
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("fabory")

LOGIN_URL  = "https://www.fabory.com/hu/login"
SEARCH_URL = "https://www.fabory.com/hu/search?text={part_no}"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "fabory_session.json"
_SESSION_MAX_AGE_H = 20


# ── Session helpers ────────────────────────────────────────────────────────────

def _load_session() -> dict | None:
    try:
        if _SESSION_FILE.exists():
            data = json.loads(_SESSION_FILE.read_text())
            if isinstance(data, dict) and "state" in data:
                return data
    except Exception:
        pass
    return None


def _session_is_fresh(session: dict) -> bool:
    saved_at = session.get("saved_at")
    if not saved_at:
        return False
    try:
        age_h = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 3600
        return age_h < _SESSION_MAX_AGE_H
    except Exception:
        return False


async def _save_session(context) -> None:
    try:
        state = await context.storage_state()
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(
            {"saved_at": datetime.now().isoformat(timespec="seconds"), "state": state},
            indent=2,
        ))
        log.info(f"Session saved → {_SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session     = _load_session()
    use_session = session and _session_is_fresh(session)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        if use_session:
            log.info("Restoring saved session (age < 20 h)")
            try:
                context = await browser.new_context(storage_state=session["state"])
            except Exception as exc:
                log.warning(f"Could not restore session state: {exc}")
                use_session = False
                _SESSION_FILE.unlink(missing_ok=True)
                context = await browser.new_context()
        else:
            if session:
                log.info("Session stale (> 20 h) — proactive re-login")
                _SESSION_FILE.unlink(missing_ok=True)
            context = await browser.new_context()

        page = await context.new_page()

        try:
            await emit("Opening fabory.com…")
            search_url = SEARCH_URL.format(part_no=supplier_part_no)

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                log.info(f"After session restore, URL: {page.url}")

                if "/login" in page.url:
                    log.warning("Session invalid — falling back to full login")
                    use_session = False
                    _SESSION_FILE.unlink(missing_ok=True)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                else:
                    log.info("Session valid — login skipped")

            # ── Step 2: full login (if needed) ───────────────────────────────
            if not use_session:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                log.info(f"Loaded login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Összes elfogadása").click(timeout=5000)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    log.info("No cookie banner appeared")

                await emit("Logging in to fabory.com…")
                username = os.getenv("SUPPLIER_E_USERNAME", "")
                log.info(f"Filling login form for user: {username}")

                await page.get_by_role("textbox", name="Email cím").fill(username)
                await page.locator("input[placeholder='Jelszó']").fill(os.getenv("SUPPLIER_E_PASSWORD", ""))
                await page.get_by_role("button", name="Belépés").click()

                try:
                    await page.wait_for_url("https://www.fabory.com/hu", timeout=15000)
                    log.info(f"Login successful: {page.url}")
                except PlaywrightTimeout:
                    log.error(f"Login failed — URL: {page.url}")
                    raise RuntimeError("Login to fabory.com failed. Please check credentials.")

                await _save_session(context)

                await emit(f"Searching for {supplier_part_no} on fabory.com…")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

            else:
                await emit(f"Searching for {supplier_part_no} on fabory.com…")

            log.info(f"Search page loaded: {page.url}")
            try:
                await page.wait_for_selector("a[href*='/p/'], text=/0 találat|no results/i", timeout=8000)
            except PlaywrightTimeout:
                log.warning("No explicit search result marker appeared after 8s — continuing anyway")

            body_text = await page.locator("body").inner_text()
            if "0 találat" in body_text or "no results" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on fabory.com.")

            if "/search" in page.url:
                log.info("On search results page, clicking first product link")
                try:
                    await page.locator("a[href*='/p/']").first.click(timeout=8000)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_function(
                        "() => document.body.innerText.includes('Ft') || document.body.innerText.includes('Készleten') || document.body.innerText.includes('Nincs készleten')",
                        timeout=10000,
                    )
                    log.info(f"Product page: {page.url}")
                except PlaywrightTimeout:
                    raise RuntimeError(f"No product links found for {supplier_part_no} on fabory.com.")
            await emit("Reading price and stock from fabory.com…")

            body_text = await page.locator("body").inner_text()
            log.info(f"Page URL: {page.url}")

            price_match = re.search(
                r"([\d][\d\s\u00a0]*)\s*Ft\s*/\s*ár\s*/\s*(\d+)",
                body_text,
            )
            if not price_match:
                raise RuntimeError("Could not read price from fabory.com. Page layout may have changed.")

            price_raw = float(re.sub(r"[\s\u00a0]", "", price_match.group(1)))
            unit_qty   = int(price_match.group(2))
            log.info(f"Price: {price_raw} Ft / {unit_qty} db")

            if "Nincs készleten" in body_text:
                stock_value = 0
            elif "Készleten" in body_text or "Raktáron" in body_text:
                stock_value = "Raktáron"
            else:
                stock_value = None
            log.info(f"Stock: {stock_value}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   unit_qty,
                "currency":         "HUF",
                "unit":             "db",
                "stock":            stock_value,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during fabory.com scrape: {exc}")
            raise RuntimeError(f"fabory.com scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
