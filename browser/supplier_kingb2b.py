"""
Playwright scraper for kingb2b.it (Supplier J)

Login flow:
  1. GET /PORTAL/ → SPA loads, shows login form or dashboard
  2. Fill username + password → login
  3. Search with the search box: "Ricerca prodotto o categoria o vostro codice se abbinato..."
  4. Navigate to product → extract price and stock

Currency: EUR (Italian supplier)
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("kingb2b")

PORTAL_URL = "https://kingb2b.it/PORTAL/"


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
            await emit("Opening kingb2b.it…")
            await page.goto(PORTAL_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)  # SPA needs time to init
            log.info(f"Portal loaded: {page.url}")

            # Check if login form is visible
            body_text = await page.locator("body").inner_text()
            if any(w in body_text.lower() for w in ["login", "accedi", "username", "password", "accesso"]):
                await emit("Logging in to kingb2b.it…")
                username = os.getenv("SUPPLIER_J_USERNAME", "")
                password = os.getenv("SUPPLIER_J_PASSWORD", "")
                log.info(f"Login form detected, logging in as: {username}")

                try:
                    await page.get_by_role("textbox", name="Username").fill(username)
                    await page.get_by_role("textbox", name="Password").fill(password)
                    await page.get_by_role("button", name="Login").click()
                except Exception:
                    # Try alternative selectors
                    await page.locator("input[type='text'], input[name*='user'], input[name*='login']").first.fill(username)
                    await page.locator("input[type='password']").first.fill(password)
                    await page.locator("button[type='submit'], input[type='submit']").first.click()

                await page.wait_for_timeout(3000)
                log.info(f"After login: {page.url}")

            # Search for part number
            await emit(f"Searching for {supplier_part_no} on kingb2b.it…")
            try:
                search_box = page.locator("input[placeholder*='Ricerca'], input[placeholder*='ricerca'], input[type='search']").first
                await search_box.fill(supplier_part_no)
                await search_box.press("Enter")
                await page.wait_for_timeout(2000)
                log.info(f"Search done: {page.url}")
            except Exception as e:
                log.warning(f"Search box interaction failed: {e}")
                raise RuntimeError(f"Could not use search on kingb2b.it for part {supplier_part_no}")

            body_text = await page.locator("body").inner_text()
            if supplier_part_no.lower() not in body_text.lower() and "nessun" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on kingb2b.it.")

            # Click first product if on results page
            try:
                await page.locator("a[href*='product'], a[href*='articol'], .product a, .item a").first.click(timeout=5000)
                await page.wait_for_timeout(2000)
                log.info(f"Product page: {page.url}")
            except PlaywrightTimeout:
                log.info("No product link click needed — might be directly on product page")

            await emit("Reading price and stock from kingb2b.it…")

            price_text = ""
            stock_text = ""

            try:
                price_el = page.locator("[class*='price'], [class*='Price'], [class*='prezzo']").first
                price_text = await price_el.inner_text(timeout=6000)
                log.info(f"Price: '{price_text}'")
            except Exception:
                body = await page.locator("body").inner_text()
                match = re.search(r"([\d.,]+)\s*€|€\s*([\d.,]+)", body)
                if match:
                    price_text = match.group(0)

            try:
                stock_el = page.locator("[class*='stock'], [class*='disponib'], [class*='giacenza']").first
                stock_text = await stock_el.inner_text(timeout=4000)
                log.info(f"Stock: '{stock_text}'")
            except Exception:
                stock_text = "unknown"

            if not price_text:
                raise RuntimeError("Could not read price from kingb2b.it. Page layout may have changed.")

            price_clean = re.sub(r"[€\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(re.search(r"[\d.]+", price_clean).group())

            unit_qty = 1
            qty_match = re.search(r"/\s*(\d+)\s*(?:pz|pcs|pc|Stk)", price_text, re.IGNORECASE)
            if qty_match:
                unit_qty = int(qty_match.group(1))

            in_stock = not any(w in stock_text.lower() for w in ["out", "esaurit", "0"])
            stock_value = 1 if in_stock else 0

            log.info(f"Parsed — price_raw: {price_raw} EUR, unit_qty: {unit_qty}, stock: {stock_value}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   unit_qty,
                "currency":         "EUR",
                "unit":             "db",
                "stock":            stock_value,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during kingb2b.it scrape: {exc}")
            raise RuntimeError(f"kingb2b.it scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
