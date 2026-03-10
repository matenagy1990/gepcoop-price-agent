"""
Playwright scraper for kingb2b.it (Supplier J)

Login flow:
  1. GET /PORTAL/ → SPA loads (wait for #header-search)
  2. Check login state: div.button-text-doc style="display:none" → not logged in
  3. Click div.header-button.account → login modal opens
  4. Fill Username + Password → click LOGIN button
  5. Confirmed when DOCUMENTI / TRACKING buttons become visible

Search flow:
  6. Fill #header-search with part number → press Enter
  7. Wait for "Attendere prego..." to disappear and div.singola-famiglia to appear
  8. Click div.singola-famiglia (family header) → expands product table
  9. Wait for tr.articoli-row[id="PART_NO"] to appear
  10. Wait for td[data-cell="PREZZO"] to be non-empty (only shown when logged in)

Price structure:
  - td[data-cell="PREZZO"] contains e.g. "0,60 %" or "7,68 %"
  - "%" → price is per 100 units (price_unit_qty = 100)
  - "N" → price is per 1 unit (price_unit_qty = 1)
  - Italian decimal: comma → dot

Stock structure:
  - td[data-cell="STOCK"] contains divs:
    - div.dispo-ok   → current stock (e.g. "26.000" = 26,000 units)
    - div.dispo-incoming → incoming stock + date (e.g. "492.000 13/05/26")
    - div.dispo-ko   → out of stock message
  - Parse first number from whichever div has content
  - Italian thousands separator: dot → strip

Currency: EUR
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


def _parse_eur(text: str) -> float:
    """Parse Italian price string: '0,60' or '7,68' → float"""
    clean = text.strip().replace(".", "").replace(",", ".")
    return float(re.search(r"[\d.]+", clean).group())


def _parse_stock(text: str) -> int:
    """Parse Italian stock: '492.000 13/05/26' → 492000, '26.000' → 26000"""
    m = re.search(r"[\d.]+", text)
    if not m:
        return 0
    return int(m.group().replace(".", ""))


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
            await emit("Opening kingb2b.it…")
            await page.goto(PORTAL_URL, wait_until="domcontentloaded")
            # Wait for SPA to fully initialise
            await page.wait_for_selector("#header-search", timeout=15000)
            await page.wait_for_timeout(1000)
            log.info("Portal loaded")

            # ── Check if login is required ─────────────────────────────
            doc_btn = page.locator("div.button-text-doc")
            is_hidden = await doc_btn.evaluate(
                "el => el.style.display === 'none' || getComputedStyle(el).display === 'none'"
            )

            if is_hidden:
                await emit("Logging in to kingb2b.it…")
                username = os.getenv("SUPPLIER_J_USERNAME", "")
                password = os.getenv("SUPPLIER_J_PASSWORD", "")
                log.info(f"Logging in as: {username}")

                # Open login modal
                await page.locator("div.header-button.account").first.click()
                await page.wait_for_selector("input[placeholder='Username']", timeout=6000)

                await page.get_by_role("textbox", name="Username").fill(username)
                await page.get_by_role("textbox", name="Password").fill(password)
                await page.get_by_role("button", name="LOGIN").click()

                # Wait for DOCUMENTI to become visible (login confirmed)
                try:
                    await page.wait_for_function(
                        "() => getComputedStyle(document.querySelector('div.button-text-doc')).display !== 'none'",
                        timeout=10000,
                    )
                except PlaywrightTimeout:
                    raise RuntimeError("Login to kingb2b.it failed. Please check credentials.")

                log.info("Login successful")
            else:
                log.info("Already logged in")

            # ── Search ────────────────────────────────────────────────
            await emit(f"Searching for {supplier_part_no} on kingb2b.it…")
            search_box = page.locator("#header-search")
            await search_box.fill(supplier_part_no)
            await search_box.press("Enter")

            # Wait for loading to finish and results to appear
            await page.wait_for_selector("div.singola-famiglia", timeout=12000)
            await page.wait_for_timeout(500)
            log.info("Search results loaded")

            # Check for "not found"
            body_text = await page.locator("body").inner_text()
            if "nessun risultato" in body_text.lower() or "nessun articolo" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on kingb2b.it.")

            # ── Expand family row ──────────────────────────────────────
            family_row = page.locator("div.singola-famiglia").first
            await family_row.click()

            # Wait for the specific article row to appear
            article_row = page.locator(f'tr.articoli-row[id="{supplier_part_no}"]')
            try:
                await article_row.wait_for(timeout=10000)
            except PlaywrightTimeout:
                raise RuntimeError(
                    f"Part {supplier_part_no} not found in the expanded product table on kingb2b.it."
                )

            # ── Wait for price to be injected (requires login) ─────────
            await emit("Reading price and stock from kingb2b.it…")
            try:
                await page.wait_for_function(
                    f"""() => {{
                        const row = document.querySelector('tr.articoli-row[id="{supplier_part_no}"]');
                        return row && row.querySelector('td[data-cell="PREZZO"]')?.innerText.trim() !== '';
                    }}""",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(
                    "Could not read price from kingb2b.it — PREZZO cell did not populate. "
                    "Check login status."
                )

            # ── Extract price ──────────────────────────────────────────
            prezzo_text = await article_row.locator('td[data-cell="PREZZO"]').inner_text()
            prezzo_text = prezzo_text.strip()
            log.info(f"PREZZO cell: {prezzo_text!r}")

            # Parse price value (Italian decimal comma)
            price_raw = _parse_eur(prezzo_text)

            # Determine unit: "%" → per 100 pcs, "N" → per 1 pc
            if "%" in prezzo_text:
                price_unit_qty = 100
            elif "N" in prezzo_text:
                price_unit_qty = 1
            else:
                # Fallback: use BOX column quantity
                box_text = await article_row.locator('td[data-cell="BOX"]').inner_text()
                box_text = box_text.strip().replace(".", "")
                try:
                    price_unit_qty = int(box_text)
                except ValueError:
                    price_unit_qty = 1
            log.info(f"Price: {price_raw} EUR / {price_unit_qty} pcs")

            # ── Extract stock ──────────────────────────────────────────
            stock_value = 0
            stock_cell = article_row.locator('td[data-cell="STOCK"]')

            for cls in ["dispo-ok", "dispo-incoming"]:
                div_text = (await stock_cell.locator(f".{cls}").inner_text()).strip()
                if div_text:
                    stock_value = _parse_stock(div_text)
                    log.info(f"Stock from .{cls}: {div_text!r} → {stock_value}")
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
            log.exception(f"Unexpected error during kingb2b.it scrape: {exc}")
            raise RuntimeError(f"kingb2b.it scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
