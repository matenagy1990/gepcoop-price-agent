"""
Playwright scraper for csavarda.hu

Login flow:
  1. GET /bejelentkezes → fill #email + #password by id → submit
  2. Redirected to /telephely-valasztasa → select Budapest (/pest)
  3. Search: /pest/kereso?search={supplier_part_no}
  4. Click first product link → side drawer opens
  5. Extract Nettó egységár, Készlet (Budapest + Vecsés)
"""

import asyncio
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

log = logging.getLogger("csavarda")

LOGIN_URL    = "https://csavarda.hu/bejelentkezes"
HOME_URL     = "https://csavarda.hu/pest"
SEARCH_URL   = "https://csavarda.hu/pest/kereso?search={part_no}"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "csavarda_session.json"


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
    if "bejelentkezes" in page.url:
        return False
    # Kosaram (cart) link is only present when authenticated
    return await page.locator("a[href*='/kosar']").count() > 0

_JS_NEXT_SIBLING = """
(labelText) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.trim() === labelText)
            return el.nextElementSibling?.textContent?.trim() ?? null;
    }
    return null;
}
"""

_JS_CONTAINS = """
(substr) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.includes(substr))
            return el.textContent.trim();
    }
    return null;
}
"""


async def _do_login(page, emit) -> None:
    """Full login flow: login page → select Budapest location."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.info(f"Loaded login page: {page.url}")
    try:
        await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
        await page.wait_for_timeout(800)
        log.info("Cookie banner accepted")
    except PlaywrightTimeout:
        log.info("No cookie banner appeared")

    await emit("Logging in to csavarda.hu…")
    username = os.getenv("SUPPLIER_A_USERNAME", "")
    log.info(f"Filling login form for user: {username}")
    await page.get_by_role("textbox", name="Email cím").fill(username)
    await page.get_by_role("textbox", name="Jelszó").fill(os.getenv("SUPPLIER_A_PASSWORD", ""))
    await page.get_by_role("button", name="Bejelentkezés").click()

    try:
        await page.wait_for_url("**/telephely-valasztasa", timeout=12000)
        log.info(f"Login successful: {page.url}")
    except PlaywrightTimeout:
        raise RuntimeError("Login to csavarda.hu failed. Please check credentials.")

    await page.get_by_role("link", name=re.compile("Budapesti telephely")).click()
    await page.wait_for_url("**/pest", timeout=10000)
    log.info(f"Location selected: {page.url}")


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()
        page.on("dialog", lambda d: (log.info(f"Dialog dismissed: {d.message}"), d.accept()))

        try:
            await emit("Opening csavarda.hu…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await _do_login(page, emit)
                    _save_cookies(await context.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await _do_login(page, emit)
                _save_cookies(await context.cookies())

            # 4. Search for part
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await emit(f"Searching for part {supplier_part_no} on csavarda.hu…")
            log.info(f"Navigating to search URL: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            # Wait for search results to render (product links or zero-results text)
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.ensure_future(page.wait_for_selector("a[href*='/pest/termek/']", timeout=15000)),
                        asyncio.ensure_future(page.wait_for_selector("text=0 találat", timeout=15000)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
            except Exception:
                pass  # will be caught below by zero_results / product_links checks
            log.info(f"Search page loaded: {page.url}")

            zero_results = await page.locator("text=0 találat").count()
            log.info(f"Zero-results indicator count: {zero_results}")
            if zero_results > 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on csavarda.hu.")

            # Count product links found
            product_links = await page.locator("a[href*='/pest/termek/']").count()
            log.info(f"Product links found on search page: {product_links}")
            if product_links == 0:
                raise RuntimeError(f"No product links found for part {supplier_part_no} on csavarda.hu.")

            # 5. Open product drawer
            log.info("Clicking first product link to open drawer…")
            await page.locator("a[href*='/pest/termek/']").first.click()

            try:
                await page.wait_for_selector("text=Nettó egységár:", timeout=8000)
                log.info("Product drawer opened — 'Nettó egységár:' label found")
            except PlaywrightTimeout:
                log.error("Price label 'Nettó egységár:' not found after clicking product")
                raise RuntimeError(
                    "csavarda.hu page layout may have changed — price selector not found."
                )

            # 6. Extract price and stock
            await emit("Reading price and stock from csavarda.hu…")
            price_str = await page.evaluate(_JS_NEXT_SIBLING, "Nettó egységár:")
            buda_str  = await page.evaluate(_JS_CONTAINS, "Budapest:")
            vecs_str  = await page.evaluate(_JS_CONTAINS, "Vecsés:")

            log.info(f"Raw extracted values — price: '{price_str}', budapest: '{buda_str}', vecsés: '{vecs_str}'")

            if not price_str:
                raise RuntimeError(
                    "Could not read price from csavarda.hu. Page layout may have changed."
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
                "stock": {
                    "budapest": parse_stock_string(buda_str) if buda_str else 0,
                    "vecsés":   parse_stock_string(vecs_str) if vecs_str else 0,
                },
                "queried_at": datetime.now().isoformat(timespec="seconds"),
            }
            log.info(f"Final result: {result}")
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during csavarda.hu scrape: {exc}")
            raise RuntimeError(f"csavarda.hu scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
