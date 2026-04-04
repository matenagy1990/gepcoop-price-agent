"""
Playwright scraper for irontrade.hu

Session flow:
  1. Try to restore saved session from assets/sessions/irontrade_session.json
     - Skip restore if saved_at > 20 h old
  2. Verify session with a positive authenticated marker:
     look for 'Kijelentkezés' link that only appears for logged-in users
  3. If session missing / stale / invalid: full login, save new session with timestamp

Login flow (fallback):
  1. GET /bejelentkezes → fill #LoginEmail + #LoginPassword → submit
  2. Livewire may return 419 (CSRF expired) → "This page has expired" dialog appears
     → accept dialog → page reloads → fill and submit again
  3. Redirects to https://irontrade.hu/ on success

Search flow:
  4. /kereso?name={supplier_part_no}
  5. Click first product link → full product page
  6. Extract Nettó ár, Készlet
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh

load_dotenv()

log = logging.getLogger("irontrade")

LOGIN_URL    = "https://irontrade.hu/bejelentkezes"
SEARCH_URL   = "https://irontrade.hu/kereso?name={part_no}"
_SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "irontrade_session.json"
_SESSION_MAX_AGE_H = 20

_JS_NEXT_SIBLING = """
(labelText) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.trim() === labelText)
            return el.nextElementSibling?.textContent?.trim() ?? null;
    }
    return null;
}
"""


async def _is_logged_in(page) -> bool:
    """
    Session check: if the restored session is valid, irontrade.hu serves the search
    page directly. If invalid, it redirects to /bejelentkezes (login page).
    URL check is instant and more reliable than waiting for a DOM element.
    """
    return "/bejelentkezes" not in page.url


# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ── Step 1: try to restore saved session ──────────────────────────────
        session  = load_session(_SESSION_FILE)
        context  = None
        skip_login = False

        if session and session_is_fresh(session, _SESSION_MAX_AGE_H):
            try:
                context = await browser.new_context(storage_state=session["state"])
                log.info("Session restored (age < 20 h) — will verify after navigation")
            except Exception as exc:
                log.warning(f"Could not restore session state: {exc}")
                invalidate_session(_SESSION_FILE)
                context = None
        elif session:
            log.info("Session stale (> 20 h) — proactive re-login")
            invalidate_session(_SESSION_FILE)

        if context is None:
            context = await browser.new_context()

        page = await context.new_page()

        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            await emit("Opening irontrade.hu…")

            # ── Step 2: verify session by navigating to search and checking auth ─
            if session and session_is_fresh(session, _SESSION_MAX_AGE_H):
                search_url = SEARCH_URL.format(part_no=supplier_part_no)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                if await _is_logged_in(page):
                    log.info("Session valid — skipping login")
                    skip_login = True
                else:
                    log.warning("Session invalid — falling back to full login")
                    invalidate_session(_SESSION_FILE)
                    await context.close()
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.on("dialog", handle_dialog)

            # ── Step 3: full login (if session missing or invalid) ────────────
            if not skip_login:
                await page.goto(LOGIN_URL, wait_until="load")
                log.info(f"Loaded login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
                    await page.wait_for_timeout(800)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    log.info("No cookie banner appeared")

                async def fill_login_form():
                    username = os.getenv("SUPPLIER_B_USERNAME", "")
                    log.info(f"Filling login form for user: {username}")
                    await page.locator("#LoginEmail").fill(username)
                    await page.locator("#LoginPassword").fill(os.getenv("SUPPLIER_B_PASSWORD", ""))
                    filled_email = await page.locator("#LoginEmail").input_value()
                    filled_pass  = await page.locator("#LoginPassword").input_value()
                    log.info(f"Form filled — email: {filled_email}, password length: {len(filled_pass)}")
                    btn = page.get_by_role("button", name="Bejelentkezés")
                    await btn.wait_for(state="visible", timeout=10000)
                    await btn.evaluate("el => el.removeAttribute('disabled')")
                    await btn.click()
                    log.info("Login button clicked")

                await emit("Logging in to irontrade.hu…")
                await fill_login_form()

                try:
                    await page.wait_for_url("https://irontrade.hu/", timeout=8000)
                    log.info(f"Login successful on first attempt: {page.url}")

                except PlaywrightTimeout:
                    log.warning(f"First login attempt timed out (likely CSRF dialog), URL: {page.url}")
                    await page.wait_for_timeout(2500)
                    log.info(f"URL after waiting for dialog reload: {page.url}")

                    if "/bejelentkezes" not in page.url:
                        log.info("Not on login page — assuming login succeeded late")
                    else:
                        log.info("Retrying login with fresh CSRF token…")
                        await fill_login_form()
                        try:
                            await page.wait_for_url("https://irontrade.hu/", timeout=12000)
                            log.info(f"Login successful on retry: {page.url}")
                        except PlaywrightTimeout:
                            current_url = page.url
                            log.error(f"Login still failed after retry — URL: {current_url}")
                            body_text = await page.locator("body").inner_text(timeout=5000)
                            for line in body_text.splitlines():
                                line = line.strip()
                                if line and any(w in line.lower() for w in ["hiba", "error", "sikertelen", "érvénytelen"]):
                                    log.error(f"Page error text: {line}")
                            raise RuntimeError("Login to irontrade.hu failed. Please check credentials.")

                try:
                    await save_session(context, _SESSION_FILE)
                except Exception as exc:
                    log.warning(f"Could not save session: {exc}")

                # ── Step 4: navigate to search after login ────────────────────
                search_url = SEARCH_URL.format(part_no=supplier_part_no)
                await emit(f"Searching for part {supplier_part_no} on irontrade.hu…")
                log.info(f"Navigating to search URL: {search_url}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

            else:
                # Already on the search page from session verify step
                await emit(f"Searching for part {supplier_part_no} on irontrade.hu…")

            log.info(f"Search page loaded: {page.url}")
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.ensure_future(page.wait_for_selector("table tbody tr", timeout=8000)),
                        asyncio.ensure_future(page.wait_for_selector("text=Találat: 0", timeout=8000)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
            except Exception:
                log.warning("No explicit search result marker appeared after 8s — continuing with body inspection")

            body_text = await page.locator("body").inner_text(timeout=10000)
            log.info(f"Search page body (first 300): {body_text[:300]}")

            if "Találat: 0" in body_text:
                log.warning(f"Zero results for part {supplier_part_no}")
                raise RuntimeError(f"Part {supplier_part_no} was not found on irontrade.hu.")

            rows = await page.locator("table tbody tr").count()
            log.info(f"Search result rows found: {rows}")

            if rows == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on irontrade.hu.")

            # ── Step 5: navigate to product page ─────────────────────────────
            log.info("Clicking first product link…")
            await page.locator("table tbody tr td a").first.click()
            await page.wait_for_load_state("domcontentloaded")
            log.info(f"Product page URL: {page.url}")

            try:
                await page.wait_for_selector("text=Nettó ár:", timeout=8000)
                log.info("Product page loaded — 'Nettó ár:' label found")
            except PlaywrightTimeout:
                log.error(f"Price label not found on product page: {page.url}")
                raise RuntimeError(
                    "irontrade.hu page layout may have changed — price selector not found."
                )

            # ── Step 6: extract price and stock ──────────────────────────────
            await emit("Reading price and stock from irontrade.hu…")
            price_str = await page.evaluate(_JS_NEXT_SIBLING, "Nettó ár:")
            stock_str = await page.evaluate(_JS_NEXT_SIBLING, "Készlet:")

            log.info(f"Raw extracted values — price: '{price_str}', stock: '{stock_str}'")

            if not price_str:
                raise RuntimeError(
                    "Could not read price from irontrade.hu. Page layout may have changed."
                )

            from agent.tools import parse_price_string, parse_stock_string
            price_raw, price_unit_qty, unit = parse_price_string(price_str)
            log.info(f"Parsed price: {price_raw} HUF / {price_unit_qty} {unit}")

            result = {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "HUF",
                "unit":             unit,
                "stock":            parse_stock_string(stock_str) if stock_str else 0,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }
            log.info(f"Final result: {result}")
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during irontrade.hu scrape: {exc}")
            raise RuntimeError(f"irontrade.hu scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
