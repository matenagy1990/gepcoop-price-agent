"""
Playwright scraper for irontrade.hu

Login flow:
  1. GET /bejelentkezes → fill #LoginEmail + #LoginPassword → submit
  2. Livewire may return 419 (CSRF expired) → "This page has expired" dialog appears
     → accept dialog → page reloads → fill and submit again
  3. Redirects to https://irontrade.hu/ on success
  4. Search: /kereso?name={supplier_part_no}
  5. Click first product link → full product page
  6. Extract Nettó ár, Készlet
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("irontrade")

LOGIN_URL    = "https://irontrade.hu/bejelentkezes"
HOME_URL     = "https://irontrade.hu/"
SEARCH_URL   = "https://irontrade.hu/kereso?name={part_no}"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "irontrade_session.json"


def _load_saved_cookies() -> list | None:
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


def _save_cookies(cookies: list) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(cookies, indent=2))
        log.info(f"Session saved to {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _is_logged_in(page) -> bool:
    return "bejelentkezes" not in page.url

_JS_NEXT_SIBLING = """
(labelText) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.trim() === labelText)
            return el.nextElementSibling?.textContent?.trim() ?? null;
    }
    return null;
}
"""


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        # Properly await async dialog acceptance
        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        async def _do_login():
            await page.goto(LOGIN_URL, wait_until="load")
            log.info(f"Loaded login page: {page.url}")
            try:
                await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
                await page.wait_for_timeout(800)
            except PlaywrightTimeout:
                pass

            async def fill_login_form():
                username = os.getenv("SUPPLIER_B_USERNAME", "")
                log.info(f"Filling login form for user: {username}")
                await page.get_by_role("textbox", name="Email").fill(username)
                await page.get_by_role("textbox", name="Jelszó").fill(os.getenv("SUPPLIER_B_PASSWORD", ""))
                btn = page.get_by_role("button", name="Bejelentkezés")
                await btn.wait_for(state="visible", timeout=10000)
                await btn.evaluate("el => el.removeAttribute('disabled')")
                await btn.click()

            await fill_login_form()
            try:
                await page.wait_for_url("https://irontrade.hu/", timeout=8000)
                log.info(f"Login successful: {page.url}")
            except PlaywrightTimeout:
                await page.wait_for_timeout(2500)
                if "/bejelentkezes" in page.url:
                    await fill_login_form()
                    try:
                        await page.wait_for_url("https://irontrade.hu/", timeout=12000)
                    except PlaywrightTimeout:
                        raise RuntimeError("Login to irontrade.hu failed. Please check credentials.")

        try:
            await emit("Opening irontrade.hu…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to irontrade.hu…")
                    await _do_login()
                    _save_cookies(await context.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to irontrade.hu…")
                await _do_login()
                _save_cookies(await context.cookies())

            # 3. Search for part
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await emit(f"Searching for part {supplier_part_no} on irontrade.hu…")
            log.info(f"Navigating to search URL: {search_url}")

            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"Search page loaded: {page.url}")

            # Wait for content to be ready
            await page.wait_for_timeout(1500)

            body_text = await page.locator("body").inner_text(timeout=10000)
            log.info(f"Search page body (first 300): {body_text[:300]}")

            if "Találat: 0" in body_text:
                log.warning(f"Zero results for part {supplier_part_no}")
                raise RuntimeError(f"Part {supplier_part_no} was not found on irontrade.hu.")

            # Count result rows
            rows = await page.locator("table tbody tr").count()
            log.info(f"Search result rows found: {rows}")

            if rows == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on irontrade.hu.")

            # 4. Navigate to product page
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

            # 5. Extract price and stock
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
