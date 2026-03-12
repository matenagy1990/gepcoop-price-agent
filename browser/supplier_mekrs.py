"""
Playwright scraper for eshop.mekrs.cz

Login flow:
  1. GET /en → fill input[name='username'] + input[name='password']
     → click [data-testid='login-button']
  2. Login form disappears on success

Search flow:
  3. Type part number into the main search box
     (input[placeholder='Search by name, code, DIN'])
  4. Wait for autocomplete dropdown → click "Show all results"
  5. Results page: /en/products?nazev={part_no}&onStock=false

Data extraction (product card layout):
  - [data-testid='product-card']   → name, stock
  - sibling div (after <hr>)       → price, unit qty
  Stock: div.text-sm.font-medium.text-primaryGreen  → "In stock 1,653,361 pcs"
  Price: span.text-primaryRed.font-bold.text-lg.leading-none → "50.63 Kč"
  Unit:  span.text-black.font-medium.text-sm.leading-none    → "/ 100 pcs"

Price normalisation:
  price_raw=50.63, price_unit_qty=100 → tools.py yields price_per_db=0.5063 CZK/db
"""

import logging
import os
import re
from datetime import datetime
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("mekrs")

LOGIN_URL = "https://eshop.mekrs.cz/en"


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            # 1. Open site and log in
            await emit("Opening eshop.mekrs.cz…")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            log.info(f"Loaded: {page.url}")

            await emit("Logging in to eshop.mekrs.cz…")
            username = os.getenv("SUPPLIER_D_USERNAME", "")
            log.info(f"Logging in as: {username}")

            await page.locator("input[name='username']").fill(username)
            await page.locator("input[name='password']").fill(os.getenv("SUPPLIER_D_PASSWORD", ""))
            await page.locator("[data-testid='login-button']").click()
            await page.wait_for_timeout(3000)

            if await page.locator("input[name='username']").count() > 0:
                raise RuntimeError("Login to eshop.mekrs.cz failed. Please check credentials.")

            log.info(f"Login successful — URL: {page.url}")

            # 2. Type the part number into the search box to trigger autocomplete
            await emit(f"Searching for part {supplier_part_no} on eshop.mekrs.cz…")
            search_inp = page.locator("input[placeholder='Search by name, code, DIN']").first
            await search_inp.click()
            await search_inp.type(supplier_part_no, delay=50)
            log.info(f"Typed '{supplier_part_no}' into search box, waiting for autocomplete…")
            await page.wait_for_timeout(2000)

            # 3. Click "Show all results" in the autocomplete dropdown
            show_all = page.locator("text=Show all results").first
            show_all_count = await show_all.count()
            if show_all_count == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(autocomplete returned no results)."
                )
            await show_all.click()
            await page.wait_for_timeout(3000)
            log.info(f"Results page loaded: {page.url}")

            # 4. Verify at least one product card exists
            card_count = await page.locator("[data-testid='product-card']").count()
            log.info(f"Product cards on results page: {card_count}")
            if card_count == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on eshop.mekrs.cz.")

            # 5. Extract data from first result
            await emit("Reading price and stock from eshop.mekrs.cz…")
            first_card = page.locator("[data-testid='product-card']").first

            # Stock lives inside the card
            try:
                stock_str = await first_card.locator(
                    "div.text-sm.font-medium.text-primaryGreen"
                ).inner_text(timeout=5000)
                stock_str = stock_str.strip()
            except PlaywrightTimeout:
                stock_str = ""
                log.warning("Stock element not found — assuming out of stock")

            # Price and unit qty — JS scan: find any leaf element whose text
            # looks like a price ("digits Kč") — class-name independent
            price_str, unit_str, price_elem_html = await page.evaluate("""() => {
                // Collect all leaf text nodes
                const candidates = [];
                for (const el of document.querySelectorAll('*')) {
                    if (el.childElementCount > 0) continue;
                    const t = el.textContent.trim();
                    candidates.push({el, t});
                }

                // Price: numeric value followed by Kč (possibly with spaces)
                let priceEl = null;
                for (const {el, t} of candidates) {
                    if (/[\\d][\\d.,]*\\s*Kč/.test(t)) {
                        priceEl = el;
                        break;
                    }
                }

                // Unit: "/ N pcs" pattern — first occurrence after price element in DOM
                let unitEl = null;
                for (const {el, t} of candidates) {
                    if (/\\/\\s*\\d[\\d,]*\\s*pcs/.test(t)) {
                        unitEl = el;
                        break;
                    }
                }

                return [
                    priceEl ? priceEl.textContent.trim() : null,
                    unitEl  ? unitEl.textContent.trim()  : null,
                    priceEl ? priceEl.outerHTML           : null,
                ];
            }""")

            log.info(f"Price element HTML: {price_elem_html}")
            log.info(
                f"Raw — price: '{price_str}', unit: '{unit_str}', stock: '{stock_str}'"
            )

            # Dump a page snapshot to help diagnose if still failing
            if not price_str:
                body_snippet = await page.evaluate(
                    "() => document.body.innerHTML.slice(0, 3000)"
                )
                log.error(f"Price not found — body HTML snippet:\n{body_snippet}")
                raise RuntimeError(
                    "Could not read price from eshop.mekrs.cz. Page layout may have changed."
                )

            # Parse price: Czech format — dot=thousands sep, comma=decimal sep
            # "50.63 Kč"   → 50.63  (dot only → treat as decimal)
            # "3.638,71 Kč"→ 3638.71 (both → remove dots, replace comma)
            # "50,63 Kč"   → 50.63  (comma only → replace comma)
            price_clean = re.sub(r"[^\d.,]", "", price_str)
            if "," in price_clean and "." in price_clean:
                price_clean = price_clean.replace(".", "").replace(",", ".")
            elif "," in price_clean:
                price_clean = price_clean.replace(",", ".")
            price_raw = float(price_clean)

            # Parse unit qty: " / 100 pcs" → 100
            qty_match = re.search(r"([\d,]+)\s*pcs", unit_str)
            if qty_match:
                price_unit_qty = int(qty_match.group(1).replace(",", ""))
            else:
                price_unit_qty = 1
                log.warning(f"Could not parse unit qty from '{unit_str}', assuming 1")

            # Parse stock: "In stock 1,653,361 pcs" → 1653361
            stock = _parse_stock(stock_str)

            log.info(f"Parsed: {price_raw} CZK / {price_unit_qty} pcs, stock: {stock}")

            result = {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "CZK",
                "unit":             "db",
                "stock":            stock,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }
            log.info(f"Final result: {result}")
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during eshop.mekrs.cz scrape: {exc}")
            raise RuntimeError(f"eshop.mekrs.cz scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")


def _parse_stock(s: str) -> int:
    """Extract stock number from 'In stock 1,653,361 pcs' → 1653361"""
    if not s:
        return 0
    digit_groups = re.findall(r"\d+", s)
    return int("".join(digit_groups)) if digit_groups else 0
