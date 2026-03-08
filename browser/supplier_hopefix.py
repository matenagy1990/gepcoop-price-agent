"""
Playwright scraper for hopefix.cz (Supplier G)

Login flow:
  1. GET /en/login → accept cookie banner → fill E-mail + Password → "Login"
  2. Redirects to /en/ or account page on success
  3. Search: /en/products?search={supplier_part_no}  (or use search box)
  4. Click first product result
  5. Extract price and stock

Currency: CZK (Czech supplier)
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

LOGIN_URL  = "https://www.hopefix.cz/en/login"
SEARCH_URL = "https://www.hopefix.cz/en/products?search={part_no}"


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
            await emit("Opening hopefix.cz…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

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
                log.error(f"Login failed — still on: {page.url}")
                raise RuntimeError("Login to hopefix.cz failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

            # Search for part
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await emit(f"Searching for {supplier_part_no} on hopefix.cz…")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"Search results: {page.url}")
            await page.wait_for_timeout(1500)

            body_text = await page.locator("body").inner_text()
            if "no product" in body_text.lower() or "0 product" in body_text.lower() or "nebyl nalezen" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on hopefix.cz.")

            # Click first product
            try:
                await page.locator("a[href*='/product/'], .product-list a, .products a").first.click(timeout=8000)
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Product page: {page.url}")
            except PlaywrightTimeout:
                raise RuntimeError(f"No product links found for {supplier_part_no} on hopefix.cz.")

            await page.wait_for_timeout(1500)
            await emit("Reading price and stock from hopefix.cz…")

            # Extract price — look for CZK price patterns
            price_text = ""
            stock_text = ""

            try:
                price_el = page.locator("[class*='price'], .product-price, [class*='Price']").first
                price_text = await price_el.inner_text(timeout=6000)
                log.info(f"Price element: '{price_text}'")
            except Exception:
                body = await page.locator("body").inner_text()
                match = re.search(r"([\d\s.,]+)\s*(?:Kč|CZK)", body)
                if match:
                    price_text = match.group(0)
                    log.info(f"Price via regex: '{price_text}'")

            try:
                stock_el = page.locator("[class*='stock'], [class*='availability'], [class*='availability']").first
                stock_text = await stock_el.inner_text(timeout=4000)
                log.info(f"Stock: '{stock_text}'")
            except Exception:
                stock_text = "unknown"

            if not price_text:
                raise RuntimeError("Could not read price from hopefix.cz. Page layout may have changed.")

            # Parse CZK price: "123,45 Kč" or "1 234,56 Kč"
            price_clean = re.sub(r"[Kč\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(re.search(r"[\d.]+", price_clean).group())

            # Check for unit qty in price string (e.g. "123 Kč / 100 ks")
            unit_qty = 1
            unit_match = re.search(r"/\s*(\d+)\s*ks", price_text, re.IGNORECASE)
            if unit_match:
                unit_qty = int(unit_match.group(1))

            in_stock = not any(w in stock_text.lower() for w in ["out", "není", "0"])
            stock_value = 1 if in_stock else 0

            log.info(f"Parsed — price_raw: {price_raw} CZK, unit_qty: {unit_qty}, stock: {stock_value}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   unit_qty,
                "currency":         "CZK",
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
