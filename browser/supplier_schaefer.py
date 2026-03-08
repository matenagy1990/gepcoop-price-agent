"""
Playwright scraper for shop.schaefer-peters.com (Supplier I)

Login flow:
  1. GET /sp/en/login/ → fill "User name" + "Webshop-Key" → "Log in"
  2. Redirects to /sp/en/home/ on success
  3. Use the search box (searchbox "Submit") to find part
  4. Click product → extract price and stock

Currency: EUR (German supplier, stainless steel fasteners)
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("schaefer")

LOGIN_URL = "https://shop.schaefer-peters.com/sp/en/login/"


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
            await emit("Opening shop.schaefer-peters.com…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

            # Login
            await emit("Logging in to shop.schaefer-peters.com…")
            username = os.getenv("SUPPLIER_I_USERNAME", "")
            password = os.getenv("SUPPLIER_I_PASSWORD", "")
            log.info(f"Logging in as: {username}")

            await page.get_by_role("textbox", name="User name").fill(username)
            await page.get_by_role("textbox", name="Webshop-Key").fill(password)
            await page.get_by_role("button", name="Log in").click()

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            if "/login" in page.url:
                log.error(f"Login failed — still on: {page.url}")
                raise RuntimeError("Login to shop.schaefer-peters.com failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

            # Search via the search box
            await emit(f"Searching for {supplier_part_no} on schaefer-peters…")
            try:
                await page.get_by_role("searchbox", name="Submit").fill(supplier_part_no)
                await page.get_by_role("button", name="Submit").click()
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Search results: {page.url}")
            except Exception as e:
                log.warning(f"Search box interaction failed: {e}")
                # Try direct URL with query
                await page.goto(f"https://shop.schaefer-peters.com/sp/en/search/?q={supplier_part_no}", wait_until="domcontentloaded")

            await page.wait_for_timeout(1500)
            body_text = await page.locator("body").inner_text()

            if "no result" in body_text.lower() or "keine Ergebnisse" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on schaefer-peters.")

            # Click first product
            try:
                await page.locator("a[href*='/product/'], a[href*='/sp/en/'], .product-name a").first.click(timeout=8000)
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Product page: {page.url}")
            except PlaywrightTimeout:
                if "/search" in page.url or "/result" in page.url:
                    raise RuntimeError(f"No product links found for {supplier_part_no} on schaefer-peters.")

            await page.wait_for_timeout(1500)
            await emit("Reading price and stock from schaefer-peters…")

            price_text = ""
            stock_text = ""

            try:
                price_el = page.locator("[class*='price'], .product-price, [class*='Price']").first
                price_text = await price_el.inner_text(timeout=6000)
                log.info(f"Price: '{price_text}'")
            except Exception:
                body = await page.locator("body").inner_text()
                match = re.search(r"([\d.,]+)\s*€|€\s*([\d.,]+)", body)
                if match:
                    price_text = match.group(0)

            try:
                stock_el = page.locator("[class*='stock'], [class*='availability'], [class*='lieferbar']").first
                stock_text = await stock_el.inner_text(timeout=4000)
                log.info(f"Stock: '{stock_text}'")
            except Exception:
                stock_text = "unknown"

            if not price_text:
                raise RuntimeError("Could not read price from schaefer-peters. Page layout may have changed.")

            price_clean = re.sub(r"[€\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(re.search(r"[\d.]+", price_clean).group())

            unit_qty = 1
            qty_match = re.search(r"/\s*(\d+)\s*(?:pcs|pc|Stk|st|pieces)", price_text, re.IGNORECASE)
            if qty_match:
                unit_qty = int(qty_match.group(1))

            in_stock = not any(w in stock_text.lower() for w in ["out", "nicht", "0"])
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
            log.exception(f"Unexpected error during schaefer-peters scrape: {exc}")
            raise RuntimeError(f"schaefer-peters scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
