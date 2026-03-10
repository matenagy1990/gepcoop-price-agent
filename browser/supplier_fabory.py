"""
Playwright scraper for fabory.com (Supplier E)

Login flow:
  1. GET /hu/login → accept cookie banner → fill email + password → "Belépés"
  2. Redirects to /hu on success
  3. Search: /hu/search?text={supplier_part_no}
  4. Click first product link
  5. Find variant row matching part_no in the variants table
  6. Extract Nettó ár (price), Ár / (unit qty), Készlet (stock)

Price format: "605 Ft" → 605 HUF per unit_qty pieces (unit_qty from "Ár /" column)
Stock format: "Készleten" = in stock, anything else = out of stock
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("fabory")

LOGIN_URL  = "https://www.fabory.com/hu/login"
SEARCH_URL = "https://www.fabory.com/hu/search?text={part_no}"


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
            await emit("Opening fabory.com…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

            # Accept cookie banner
            try:
                await page.get_by_role("button", name="Összes elfogadása").click(timeout=5000)
                await page.wait_for_timeout(800)
                log.info("Cookie banner accepted")
            except PlaywrightTimeout:
                log.info("No cookie banner appeared")

            # Login
            await emit("Logging in to fabory.com…")
            username = os.getenv("SUPPLIER_E_USERNAME", "")
            password = os.getenv("SUPPLIER_E_PASSWORD", "")
            log.info(f"Filling login form for user: {username}")

            await page.get_by_role("textbox", name="Email cím").fill(username)
            await page.locator("input[placeholder='Jelszó']").fill(password)
            await page.get_by_role("button", name="Belépés").click()

            try:
                await page.wait_for_url("https://www.fabory.com/hu", timeout=15000)
                log.info(f"Login successful: {page.url}")
            except PlaywrightTimeout:
                body = await page.locator("body").inner_text()
                log.error(f"Login failed — URL: {page.url}")
                raise RuntimeError("Login to fabory.com failed. Please check credentials.")

            # Search for part
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await emit(f"Searching for {supplier_part_no} on fabory.com…")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"Search page loaded: {page.url}")

            await page.wait_for_timeout(2000)

            # Check for no results
            body_text = await page.locator("body").inner_text()
            if "0 találat" in body_text or "no results" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on fabory.com.")

            # If redirected directly to product page, we're done searching
            # Otherwise click first result
            if "/search" in page.url:
                log.info("On search results page, clicking first product link")
                try:
                    await page.locator("a[href*='/p/']").first.click(timeout=8000)
                    await page.wait_for_load_state("domcontentloaded")
                    log.info(f"Product page: {page.url}")
                except PlaywrightTimeout:
                    raise RuntimeError(f"No product links found for {supplier_part_no} on fabory.com.")

            await page.wait_for_timeout(2000)
            await emit("Reading price and stock from fabory.com…")

            body_text = await page.locator("body").inner_text()
            log.info(f"Page URL: {page.url}")

            # Price format on product page: "26 000 Ft / ár / 100"
            # Match: price (with space as thousands sep) + "Ft / ár /" + unit_qty
            price_match = re.search(
                r"([\d][\d\s\u00a0]*)\s*Ft\s*/\s*ár\s*/\s*(\d+)",
                body_text
            )
            if not price_match:
                raise RuntimeError("Could not read price from fabory.com. Page layout may have changed.")

            price_raw = float(re.sub(r"[\s\u00a0]", "", price_match.group(1)))
            unit_qty   = int(price_match.group(2))
            log.info(f"Price: {price_raw} Ft / {unit_qty} db")

            # Stock — Fabory only shows availability, not an exact quantity
            if "Nincs készleten" in body_text:
                stock_value = 0
            elif "Készleten" in body_text or "Raktáron" in body_text:
                stock_value = "Raktáron"
            else:
                stock_value = None  # No stock information on this page
            log.info(f"Stock: {stock_value}")

            log.info(f"Parsed — price_raw: {price_raw} HUF / {unit_qty} db, stock: {stock_value}")

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
