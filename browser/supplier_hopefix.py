"""
Playwright scraper for hopefix.cz (Supplier G)

Session flow:
  1. Try to restore saved session from assets/sessions/hopefix_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to /en — if redirected to /en/login → session invalid
  3. If session missing / stale / invalid: full login, save new session

Login flow (fallback):
  1. GET /en/login → accept cookie banner → fill E-mail + Password → "Login"
  2. Redirects to /en/products on success

Search flow:
  4. Type part number into the autocomplete search box (#search_input)
  5. Wait for the jQuery UI autocomplete dropdown (#ui-id-1) to appear
  6. Click the suggestion that matches the part number
     → navigates to /en/products/{slug}#{part_no}
  7. Find the table row whose text contains the part number
  8. Extract EUR price from the cell containing '€' and stock from the cell before it

Currency: EUR
Price column: "EUR/100 pcs" → price_unit_qty = 100
Stock column: "Stock (100 pcs)" — raw value stored as int
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

log = logging.getLogger("hopefix")

LOGIN_URL  = "https://www.hopefix.cz/en/login"
HOME_URL   = "https://www.hopefix.cz/en"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "hopefix_session.json"
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
            await emit("Opening hopefix.cz…")

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
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
                log.info(f"Login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Vše přijmout").click(timeout=5000)
                    await page.wait_for_timeout(600)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    try:
                        await page.get_by_role("button", name="Accept all").click(timeout=3000)
                        await page.wait_for_timeout(600)
                    except PlaywrightTimeout:
                        log.info("No cookie banner")

                await emit("Logging in to hopefix.cz…")
                username = os.getenv("SUPPLIER_G_USERNAME", "")
                password = os.getenv("SUPPLIER_G_PASSWORD", "")
                log.info(f"Logging in as: {username}")

                await page.get_by_role("textbox", name="E-mail").fill(username)
                await page.get_by_role("textbox", name="Password").fill(password)
                await page.get_by_role("button", name="Login").click()

                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1500)

                if "/login" in page.url:
                    raise RuntimeError("Login to hopefix.cz failed. Please check credentials.")
                log.info(f"Login successful: {page.url}")

                await _save_session(context)

            # ── Step 3: search via autocomplete ──────────────────────────────
            await emit(f"Searching for {supplier_part_no} on hopefix.cz…")
            search_box = page.locator("#search_input")
            await search_box.click()
            await search_box.type(supplier_part_no, delay=60)
            log.info("Typed part number, waiting for autocomplete…")

            try:
                await page.wait_for_selector(
                    f"#ui-id-1 li:has-text('{supplier_part_no}')",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on hopefix.cz "
                    "(no autocomplete suggestion appeared)."
                )

            suggestion = page.locator("#ui-id-1 li").filter(has_text=supplier_part_no).first
            await suggestion.click(timeout=5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                log.warning("networkidle timeout — continuing anyway")
            await page.wait_for_timeout(500)
            log.info(f"Product page: {page.url}")

            await emit("Reading price and stock from hopefix.cz…")

            row = page.locator("tr").filter(has_text=supplier_part_no).first
            if await row.count() == 0:
                raise RuntimeError(f"Part {supplier_part_no} row not found on hopefix.cz.")

            toggle = row.locator(".toggle-expander")
            if await toggle.count() == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} has no pricing available on hopefix.cz "
                    "(product may be out of stock or not offered to this account)."
                )

            await toggle.click()
            await page.wait_for_timeout(500)

            expander = page.locator(
                f"form:has(input[name='product_nr'][value='{supplier_part_no}'])"
            )
            box_option = expander.locator("select.package_type option").first
            data_price = await box_option.get_attribute("data-price")
            data_qty   = await box_option.get_attribute("data-qty")
            log.info(f"Expander data-price={data_price!r}, data-qty={data_qty!r}")

            if not data_price or not data_qty or float(data_price) == 0:
                raise RuntimeError(f"Part {supplier_part_no} has no pricing available on hopefix.cz.")

            price_raw      = float(data_price)
            price_unit_qty = int(round(float(data_qty) * 100))

            stock_value = 0
            cells = row.locator("td")
            if await cells.count() > 6:
                stock_text = (await cells.nth(6).inner_text()).strip()
                log.info(f"Stock cell text: {stock_text!r}")
                m = re.search(r"[\d]+(?:[.,][\d]+)?", stock_text)
                if m:
                    stock_value = int(float(m.group().replace(",", "."))) * 100

            log.info(f"Parsed — {price_raw} EUR / {price_unit_qty} pcs, stock: {stock_value}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "EUR",
                "unit":             "db",
                "stock":            stock_value,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during hopefix.cz scrape: {exc}")
            raise RuntimeError(f"hopefix.cz scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
