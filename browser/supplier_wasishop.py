"""
Playwright scraper for wasishop.de (Supplier K)

Login flow:
  1. GET /login_form.php → dismiss cookie banner → fill Name + Passwort → "Anmelden"
  2. Redirects to /de/ or account page on success
  3. Search via search box "Suche"
  4. Click first product → extract price and stock

Currency: EUR (German supplier, stainless steel)
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("wasishop")

LOGIN_URL = "https://www.wasishop.de/login_form.php"


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
            await emit("Opening wasishop.de…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info(f"Loaded login page: {page.url}")

            # Dismiss cookie banner
            try:
                await page.get_by_role("button", name="OK").click(timeout=5000)
                await page.wait_for_timeout(500)
                log.info("Cookie banner dismissed")
            except PlaywrightTimeout:
                log.info("No cookie banner")

            # Login
            await emit("Logging in to wasishop.de…")
            username = os.getenv("SUPPLIER_K_USERNAME", "")
            password = os.getenv("SUPPLIER_K_PASSWORD", "")
            log.info(f"Logging in as: {username}")

            await page.get_by_role("textbox", name="Name").fill(username)
            await page.get_by_role("textbox", name="Passwort").fill(password)
            await page.get_by_role("button", name="Anmelden").click()

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)

            if "login_form" in page.url:
                log.error(f"Login failed — still on: {page.url}")
                raise RuntimeError("Login to wasishop.de failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

            # Search for part number
            await emit(f"Searching for {supplier_part_no} on wasishop.de…")
            try:
                await page.get_by_role("searchbox", name="Suche").fill(supplier_part_no)
                await page.get_by_role("searchbox", name="Suche").press("Enter")
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Search results: {page.url}")
            except Exception as e:
                log.warning(f"Search box failed: {e}")
                await page.goto(f"https://www.wasishop.de/de/search?q={supplier_part_no}", wait_until="domcontentloaded")

            await page.wait_for_timeout(1500)
            body_text = await page.locator("body").inner_text()

            if "kein Ergebnis" in body_text or "keine Artikel" in body_text or "0 Artikel" in body_text:
                raise RuntimeError(f"Part {supplier_part_no} was not found on wasishop.de.")

            # Click first product
            try:
                await page.locator("a[href*='/de/artikel/'], a[href*='/produkt/'], .product-item a, .artikel a").first.click(timeout=8000)
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"Product page: {page.url}")
            except PlaywrightTimeout:
                if "search" in page.url or "Artikelliste" in page.url:
                    raise RuntimeError(f"No product links found for {supplier_part_no} on wasishop.de.")

            await page.wait_for_timeout(1500)
            await emit("Reading price and stock from wasishop.de…")

            price_text = ""
            stock_text = ""

            try:
                price_el = page.locator("[class*='price'], .product-price, [class*='Price'], [class*='preis']").first
                price_text = await price_el.inner_text(timeout=6000)
                log.info(f"Price: '{price_text}'")
            except Exception:
                body = await page.locator("body").inner_text()
                match = re.search(r"([\d.,]+)\s*€|€\s*([\d.,]+)", body)
                if match:
                    price_text = match.group(0)

            try:
                stock_el = page.locator("[class*='stock'], [class*='verfügbar'], [class*='lager'], [class*='lieferbar']").first
                stock_text = await stock_el.inner_text(timeout=4000)
                log.info(f"Stock: '{stock_text}'")
            except Exception:
                stock_text = "unknown"

            if not price_text:
                raise RuntimeError("Could not read price from wasishop.de. Page layout may have changed.")

            price_clean = re.sub(r"[€\s\u00a0]", "", price_text).strip()
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(re.search(r"[\d.]+", price_clean).group())

            unit_qty = 1
            qty_match = re.search(r"/\s*(\d+)\s*(?:Stk|st|pcs|pc|pieces|VPE)", price_text, re.IGNORECASE)
            if qty_match:
                unit_qty = int(qty_match.group(1))

            in_stock = not any(w in stock_text.lower() for w in ["nicht", "out", "0"])
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
            log.exception(f"Unexpected error during wasishop.de scrape: {exc}")
            raise RuntimeError(f"wasishop.de scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
