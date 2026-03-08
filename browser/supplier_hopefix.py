"""
Playwright scraper for hopefix.cz (Supplier G)

Login flow:
  1. GET /en/login → accept cookie banner → fill E-mail + Password → "Login"
  2. Redirects to /en/products on success

Search flow:
  3. Type part number into the autocomplete search box (#search_input)
  4. Wait for the jQuery UI autocomplete dropdown (#ui-id-1) to appear
  5. Click the suggestion that matches the part number
     → navigates to /en/products/{slug}#{part_no}
  6. Find the table row whose text contains the part number
  7. Extract EUR price from the cell containing '€' and stock from the cell before it

Currency: EUR
Price column: "EUR/100 pcs" → price_unit_qty = 100
Stock column: "Stock (100 pcs)" — raw value stored as int
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("hopefix")

LOGIN_URL = "https://www.hopefix.cz/en/login"


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        try:
            await emit("Opening hopefix.cz…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Login page: {page.url}")

            # Accept cookie banner
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

            # Login
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

            # Search via autocomplete search box
            await emit(f"Searching for {supplier_part_no} on hopefix.cz…")
            search_box = page.locator("#search_input")
            await search_box.fill(supplier_part_no)
            log.info("Typed part number, waiting for autocomplete…")

            # Wait for autocomplete dropdown to appear with a matching suggestion
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

            # Click the suggestion
            suggestion = page.locator("#ui-id-1 li").filter(has_text=supplier_part_no).first
            await suggestion.click(timeout=5000)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)
            log.info(f"Product page: {page.url}")

            await emit("Reading price and stock from hopefix.cz…")

            # Find the table row whose text contains the part number
            row = page.locator("tr").filter(has_text=supplier_part_no).first
            if await row.count() == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} row not found in product table on hopefix.cz."
                )

            row_text = await row.inner_text()
            log.info(f"Matched row: {row_text!r}")

            # --- Price: cell containing '€' ---
            price_cell = row.locator("td").filter(has_text="€").first
            if await price_cell.count() == 0:
                raise RuntimeError("EUR price cell not found in matched row on hopefix.cz.")

            price_text = (await price_cell.inner_text(timeout=5000)).strip()
            log.info(f"Price cell: {price_text!r}")

            # Parse: "13,31 €" or "13,31\xa0€"
            price_clean = re.sub(r"[€\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(price_clean)

            # Column header is "EUR/100 pcs" — always 100 pieces
            price_unit_qty = 100

            # --- Stock: cell immediately before the EUR cell ---
            cells = row.locator("td")
            cell_count = await cells.count()
            stock_value = 0
            for i in range(cell_count):
                cell_text = (await cells.nth(i).inner_text()).strip()
                if "€" in cell_text and i > 0:
                    stock_text = (await cells.nth(i - 1).inner_text()).strip()
                    log.info(f"Stock cell: {stock_text!r}")
                    m = re.search(r"[\d]+(?:[.,][\d]+)?", stock_text)
                    if m:
                        stock_value = int(float(m.group().replace(",", ".")))
                    break

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
