"""
Playwright scraper for rio.reyher.de (Supplier F)

Login flow:
  1. Try to restore session from assets/sessions/reyher_session.json
  2. If session invalid/missing: login with credentials, save new session
  3. Fill Cikkszám search field → press Enter
  4. Extract price and packaging qty from search results table

Session persistence:
  Cookies are saved after each successful login. On the next run the
  saved cookies are restored so that login is skipped entirely.  Only
  when the session has expired (site shows "Csak vendéghozzáféréssel
  rendelkezik") is a fresh login performed.

Currency: EUR (German supplier)
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

log = logging.getLogger("reyher")

LOGIN_URL    = "https://rio.reyher.de/hu/customer/account/login"
HOME_URL     = "https://rio.reyher.de/hu/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "reyher_session.json"


def _load_saved_cookies() -> list | None:
    """Return saved cookies or None if not present / unreadable."""
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


def _save_cookies(cookies: list) -> None:
    """Persist cookies to disk."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(cookies, indent=2))
        log.info(f"Session saved to {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _is_logged_in(page) -> bool:
    """Return True if the current page shows an authenticated session."""
    body = await page.locator("body").inner_text()
    return "Bejelentkezve" in body or "Fiókom" in body


async def _login(page, emit) -> None:
    """Perform a full login and wait for the home page."""
    customer_code = os.getenv("SUPPLIER_F_CUSTOMER_CODE", "")
    username      = os.getenv("SUPPLIER_F_USERNAME", "")
    password      = os.getenv("SUPPLIER_F_PASSWORD", "")
    log.info(f"Logging in as customer {customer_code} / user {username}")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.info(f"Login page loaded: {page.url}")

    # Accept cookie banner
    try:
        await page.get_by_role("button", name="Allow all").click(timeout=5000)
        await page.wait_for_timeout(800)
        log.info("Cookie banner accepted")
    except PlaywrightTimeout:
        pass

    await emit("Logging in to rio.reyher.de…")
    await page.get_by_role("textbox", name="Ügyfélszám").fill(customer_code)
    await page.get_by_role("textbox", name="Felhasználónév").fill(username)
    await page.get_by_role("textbox", name="Jelszó").fill(password)
    await page.get_by_role("button", name="Bejelentkezés").click()

    try:
        await page.wait_for_url(HOME_URL, timeout=15000)
        log.info(f"Login successful: {page.url}")
    except PlaywrightTimeout:
        raise RuntimeError("Login to rio.reyher.de failed. Please check credentials.")


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        try:
            await emit("Opening rio.reyher.de…")

            # --- 1. Restore saved session or do fresh login ---
            # The homepage always shows guest state for headless browsers
            # (CDN-cached / bot detection), so we skip auth verification there
            # and rely on the two-strategy price extraction instead.
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies — skipping login")
                await context.add_cookies(saved_cookies)
                # Navigate to homepage so the Cikkszám search field is accessible
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            else:
                await _login(page, emit)
                cookies = await context.cookies()
                _save_cookies(cookies)

            # --- 3. Search ---
            await emit(f"Searching for {supplier_part_no} on rio.reyher.de…")
            await page.get_by_role("textbox", name="Cikkszám").fill(supplier_part_no)
            await page.get_by_role("textbox", name="Cikkszám").press("Enter")
            await page.wait_for_load_state("networkidle", timeout=25000)
            log.info(f"Search results: {page.url}")

            # Wait for authenticated price column to render
            try:
                await page.wait_for_function(
                    r"document.body.innerText.match(/\d[,.\d]*\s*EUR/)",
                    timeout=20000,
                )
                log.info("Numeric EUR price detected in DOM")
            except PlaywrightTimeout:
                log.warning("Price not in DOM after 20s — falling back to HTML source")

            # --- 4. Extract price ---
            body_text = await page.locator("body").inner_text()
            raw_html  = await page.content()
            log.info(f"Body (first 500 chars): {body_text[:500]}")

            if "Nem található" in body_text or "0 találat" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            await emit("Reading price and stock from rio.reyher.de…")

            price_unit_qty = None
            price_text     = None

            # Strategy 1: rendered DOM — table row "200\n55,00 EUR"
            qty_price_match = re.search(
                r"(\d+)\s*\n\s*([\d]{1,3}(?:[.,\u00a0 ]?\d{3})*[.,]\d{2})[\s\u00a0]*EUR",
                body_text
            )
            if qty_price_match:
                price_unit_qty = int(qty_price_match.group(1))
                price_text     = qty_price_match.group(2)
                log.info("Price extracted from DOM text")
            else:
                # Strategy 2: server-rendered HTML JSON
                # price&quot;:&quot;55,00\u00a0EUR&quot;
                html_match = re.search(
                    r'price&quot;:&quot;([\d,\.]+)(?:\\u00a0|&nbsp;|\u00a0| )EUR&quot;',
                    raw_html
                )
                if html_match:
                    price_text = html_match.group(1)
                    qty_html = re.search(
                        r'(?:qty_csp|packaging_qty|qty_min|minQty)&quot;:&quot;(\d+)',
                        raw_html
                    )
                    price_unit_qty = int(qty_html.group(1)) if qty_html else 200
                    log.info(f"Price extracted from HTML source (qty={price_unit_qty})")
                else:
                    log.error(f"Body sample: {body_text[:500]}")
                    raise RuntimeError("Could not read price from rio.reyher.de. No EUR amount found.")

            if "," in price_text and "." in price_text:
                price_text = price_text.replace(".", "").replace(",", ".")
            elif "," in price_text:
                price_text = price_text.replace(",", ".")
            price_raw = float(price_text)

            stock_value = None  # Reyher does not publish stock quantities
            log.info(f"Parsed — price_raw: {price_raw} EUR / {price_unit_qty} db, stock: {stock_value}")

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
            log.exception(f"Unexpected error during rio.reyher.de scrape: {exc}")
            raise RuntimeError(f"rio.reyher.de scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
