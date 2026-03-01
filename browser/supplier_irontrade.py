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
import logging
import os
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("irontrade")

LOGIN_URL  = "https://irontrade.hu/bejelentkezes"
SEARCH_URL = "https://irontrade.hu/kereso?name={part_no}"

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

        try:
            # 1. Open login page
            await emit("Opening irontrade.hu…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

            # Accept cookie banner if present
            try:
                await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
                await page.wait_for_timeout(800)
                log.info("Cookie banner accepted")
            except PlaywrightTimeout:
                log.info("No cookie banner appeared")

            # 2. Fill login form using stable id selectors
            async def fill_login_form():
                username = os.getenv("SUPPLIER_B_USERNAME", "")
                log.info(f"Filling login form for user: {username}")
                await page.locator("#LoginEmail").fill(username)
                await page.locator("#LoginPassword").fill(os.getenv("SUPPLIER_B_PASSWORD", ""))
                filled_email = await page.locator("#LoginEmail").input_value()
                filled_pass  = await page.locator("#LoginPassword").input_value()
                log.info(f"Form filled — email: {filled_email}, password length: {len(filled_pass)}")
                await page.get_by_role("button", name="Bejelentkezés").click()
                log.info("Login button clicked")

            await emit("Logging in to irontrade.hu…")
            await fill_login_form()

            # First attempt: wait for redirect to homepage
            try:
                await page.wait_for_url("https://irontrade.hu/", timeout=8000)
                log.info(f"Login successful on first attempt: {page.url}")

            except PlaywrightTimeout:
                # Livewire 419 CSRF expiry causes a dialog → page reloads back to /bejelentkezes
                # The dialog handler already accepted it; wait for the reload to settle
                log.warning(f"First login attempt timed out (likely CSRF dialog), URL: {page.url}")
                await page.wait_for_timeout(2500)
                log.info(f"URL after waiting for dialog reload: {page.url}")

                if "/bejelentkezes" not in page.url:
                    # Might have already redirected (slow network)
                    log.info("Not on login page — assuming login succeeded late")
                else:
                    # Page reloaded with fresh CSRF token — try again
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
