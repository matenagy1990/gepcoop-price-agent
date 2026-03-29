"""
Playwright scraper for fbonline.fastbolt.com (Supplier H)

Login flow:
  1. GET /login → fill Shortname + Loginname + Password → "Sign in"
  2. Redirects to dashboard on success

Search + price flow:
  3. Navigate to /matrix/{supplier_part_no}
     → shows a size/variant matrix table; the exact part is highlighted as a clickable cell
  4. Click the matrix cell: generic[title="{supplier_part_no}"]
     → adds the article to the enquiry panel (bottom of page)
  5. Wait for input#enquiry-item-{supplier_part_no} to appear in the enquiry panel
  6. Clear the quantity input and type "1", then click a.btn-change-amount (fa-check)
     → triggers AJAX price recalculation
  7. Wait for div.current-amount to contain EUR text

Data extraction (enquiry panel layout after quantity confirm):
  - Price:     div.current-amount span.text-red inner text → "10.58 EUR\n/ 100\n= 105.80 EUR"
  - Unit qty:  "/ 100" extracted from that text
  - Stock:     span.progress-bar-success inner text        → "1,000" (in-stock qty)

Price normalisation:
  price_raw=10.58, price_unit_qty=100 → tools.py yields price_per_db=0.1058 EUR/db

Currency: EUR
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("fastbolt")

LOGIN_URL    = "https://fbonline.fastbolt.com/login"
HOME_URL     = "https://fbonline.fastbolt.com/"
MATRIX_URL   = "https://fbonline.fastbolt.com/matrix/{part_no}"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "fastbolt_session.json"


def _load_saved_cookies() -> list | None:
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


def _save_cookies(cookies: list) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(cookies, indent=2))
        log.info(f"Session saved to {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _is_logged_in(page) -> bool:
    return "/login" not in page.url


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        async def _do_login():
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
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
                raise RuntimeError("Login to fbonline.fastbolt.com failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

        try:
            await emit("Opening fbonline.fastbolt.com…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to fbonline.fastbolt.com…")
                    await _do_login()
                    _save_cookies(await context.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to fbonline.fastbolt.com…")
                await _do_login()
                _save_cookies(await context.cookies())

            # Navigate to the product matrix page
            await emit(f"Searching for {supplier_part_no} on fastbolt…")
            matrix_url = MATRIX_URL.format(part_no=supplier_part_no)
            await page.goto(matrix_url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"Matrix page: {page.url}")

            # If redirected away, the part number does not exist on fastbolt
            if f"/matrix/{supplier_part_no}" not in page.url:
                raise RuntimeError(f"Part {supplier_part_no} was not found on fastbolt.")

            # Click the specific cell in the matrix table to add it to the enquiry panel
            matrix_cell = page.locator(f"[title='{supplier_part_no}']").first
            try:
                await matrix_cell.wait_for(timeout=8000)
                await matrix_cell.click()
                log.info(f"Clicked matrix cell for {supplier_part_no}")
            except PlaywrightTimeout:
                raise RuntimeError(f"Part {supplier_part_no} not found in matrix table on fastbolt.")

            # Wait for the enquiry input for this specific part to appear
            qty_selector = f"input#enquiry-item-{supplier_part_no}"
            try:
                await page.wait_for_selector(qty_selector, timeout=10000)
                log.info(f"Enquiry panel updated for {supplier_part_no}")
            except PlaywrightTimeout:
                raise RuntimeError(f"Enquiry panel did not load for {supplier_part_no} on fastbolt.")

            await emit("Reading price and stock from fastbolt…")

            # Type "1" into the quantity field and click the checkmark to trigger price load
            qty_input = page.locator(qty_selector)
            await qty_input.fill("1")
            log.info("Filled quantity with 1")

            # Click the checkmark inside the enquiry item row for this specific part
            item_locator = page.locator(f".item.enquiry-item:has(input#enquiry-item-{supplier_part_no})")
            await item_locator.locator("a.btn-change-amount").click()
            log.info("Clicked checkmark to confirm quantity")

            # Wait for the AJAX price update — span.text-red > b must contain EUR
            try:
                await page.wait_for_function(
                    "() => {"
                    "  const b = document.querySelector('.current-amount span.text-red b');"
                    "  return b && b.innerText.includes('EUR');"
                    "}",
                    timeout=10000,
                )
                log.info("Price loaded in enquiry panel")
            except PlaywrightTimeout:
                log.warning("Price did not appear after 10s — attempting extraction anyway")

            # Extract price and unit qty — scope to the specific enquiry item for this part
            price_data = await page.evaluate(f"""() => {{
                const item = document.querySelector(
                    '.item.enquiry-item:has(input#enquiry-item-{supplier_part_no})'
                );
                if (!item) return null;
                const span = item.querySelector('.current-amount span.text-red');
                if (!span) return null;
                return span.innerText.trim();   // e.g. "10.58 EUR\\n/ 100\\n= 105.80 EUR"
            }}""")

            log.info(f"Raw price text: {price_data!r}")

            if not price_data or "EUR" not in price_data:
                body = await page.locator("body").inner_text()
                log.error(f"Price not found. Body snippet: {body[:500]}")
                raise RuntimeError("Could not read price from fastbolt. Page layout may have changed.")

            # Parse: "3.10 EUR\n/ 100\n= 7.75 EUR"
            # Price value
            price_match = re.search(r"([\d.,]+)\s*EUR", price_data)
            if not price_match:
                raise RuntimeError(f"Could not parse price from: {price_data!r}")
            price_str = price_match.group(1)
            # German/English format: dots as thousands sep, comma as decimal — or just dot as decimal
            if "," in price_str and "." in price_str:
                price_str = price_str.replace(",", "")          # "1,234.56" → "1234.56"
            elif "," in price_str:
                price_str = price_str.replace(",", ".")         # "3,10" → "3.10"
            price_raw = float(price_str)

            # Unit qty: "/ 100" → 100
            qty_match = re.search(r"/\s*([\d,]+)", price_data)
            price_unit_qty = int(qty_match.group(1).replace(",", "")) if qty_match else 1
            if not qty_match:
                log.warning(f"Could not parse unit qty from {price_data!r}, assuming 1")

            # Stock: .progress-bar-success scoped to this specific enquiry item
            stock_text = await page.evaluate(f"""() => {{
                const item = document.querySelector(
                    '.item.enquiry-item:has(input#enquiry-item-{supplier_part_no})'
                );
                if (!item) return '';
                const bar = item.querySelector('.progress-bar-success');
                return bar ? bar.innerText.trim() : '';
            }}""")
            log.info(f"Stock text: {stock_text!r}")
            stock = _parse_stock(stock_text)

            log.info(f"Parsed — price_raw: {price_raw} EUR, unit_qty: {price_unit_qty}, stock: {stock}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "EUR",
                "unit":             "db",
                "stock":            stock,
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


def _parse_stock(s: str) -> int:
    """Extract integer from stock text, e.g. '1,250' → 1250, '250' → 250"""
    if not s:
        return 0
    cleaned = re.sub(r"[^\d]", "", s)
    return int(cleaned) if cleaned else 0
