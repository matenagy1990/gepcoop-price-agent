"""
Playwright scraper for fbonline.fastbolt.com (Supplier H)

Login flow:
  1. GET /login → fill Shortname + Loginname + Password → "Sign in"
  2. Redirects to dashboard on success
  3. Search for part number via search box or URL
  4. Extract price and stock

Currency: EUR (German supplier)
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("fastbolt")

LOGIN_URL  = "https://fbonline.fastbolt.com/login"
SEARCH_URL = "https://fbonline.fastbolt.com/search?q={part_no}"


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
            await emit("Opening fbonline.fastbolt.com…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

            # Login — 3 fields: Shortname, Loginname, Password
            await emit("Logging in to fbonline.fastbolt.com…")
            shortname = os.getenv("SUPPLIER_H_SHORTNAME", "")
            username  = os.getenv("SUPPLIER_H_USERNAME", "")
            password  = os.getenv("SUPPLIER_H_PASSWORD", "")
            log.info(f"Logging in — shortname: {shortname}, user: {username}")

            await page.get_by_role("searchbox", name="Shortname:").fill(shortname)
            await page.get_by_role("searchbox", name="Loginname:").fill(username)
            await page.get_by_role("textbox", name="Password:").fill(password)
            await page.get_by_role("button", name="Sign in").click()

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            if "/login" in page.url:
                log.error(f"Login failed — still on: {page.url}")
                raise RuntimeError("Login to fbonline.fastbolt.com failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

            # Search for part
            await emit(f"Searching for {supplier_part_no} on fastbolt…")
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"Search page: {page.url}")
            await page.wait_for_timeout(1500)

            body_text = await page.locator("body").inner_text()
            if "no result" in body_text.lower() or "0 result" in body_text.lower() or "nicht gefunden" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on fastbolt.")

            # Click first product link
            try:
                await page.locator("a.product-link, .product-list a, [href*='/product/'], [href*='/artikel/']").first.click(timeout=8000)
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Product page: {page.url}")
            except PlaywrightTimeout:
                # might already be on product page
                if "search" in page.url:
                    raise RuntimeError(f"No product links found for {supplier_part_no} on fastbolt.")

            await page.wait_for_timeout(1500)
            await emit("Reading price and stock from fastbolt…")

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
                stock_el = page.locator("[class*='stock'], [class*='availability']").first
                stock_text = await stock_el.inner_text(timeout=4000)
                log.info(f"Stock: '{stock_text}'")
            except Exception:
                stock_text = "unknown"

            if not price_text:
                raise RuntimeError("Could not read price from fastbolt. Page layout may have changed.")

            price_clean = re.sub(r"[€\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(re.search(r"[\d.]+", price_clean).group())

            # Check for unit qty in price (e.g. "5,00 € / 100 pcs")
            unit_qty = 1
            qty_match = re.search(r"/\s*(\d+)\s*(?:pcs|pc|Stk|st)", price_text, re.IGNORECASE)
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
            log.exception(f"Unexpected error during fastbolt scrape: {exc}")
            raise RuntimeError(f"fastbolt scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
