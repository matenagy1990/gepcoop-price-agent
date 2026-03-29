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

log = logging.getLogger("hopefix")

LOGIN_URL    = "https://www.hopefix.cz/en/login"
HOME_URL     = "https://www.hopefix.cz/en/products"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "hopefix_session.json"


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
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def _do_login():
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                await page.get_by_role("button", name="Vše přijmout").click(timeout=5000)
                await page.wait_for_timeout(600)
            except PlaywrightTimeout:
                try:
                    await page.get_by_role("button", name="Accept all").click(timeout=3000)
                    await page.wait_for_timeout(600)
                except PlaywrightTimeout:
                    pass
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

        try:
            await emit("Opening hopefix.cz…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to hopefix.cz…")
                    await _do_login()
                    _save_cookies(await context.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to hopefix.cz…")
                await _do_login()
                _save_cookies(await context.cookies())

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
            # Wait for AJAX to complete so table cells and toggle buttons are populated
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                log.warning("networkidle timeout — continuing anyway")
            await page.wait_for_timeout(500)
            log.info(f"Product page: {page.url}")

            await emit("Reading price and stock from hopefix.cz…")

            # Find the row for this part number
            row = page.locator("tr").filter(has_text=supplier_part_no).first
            if await row.count() == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} row not found on hopefix.cz."
                )

            # --- Check toggle-expander button exists (only appears when pricing is available) ---
            toggle = row.locator(".toggle-expander")
            if await toggle.count() == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} has no pricing available on hopefix.cz "
                    "(product may be out of stock or not offered to this account)."
                )

            # Click toggle to open the expander row
            await toggle.click()
            await page.wait_for_timeout(500)

            # --- Price: read data-price / data-qty from the Box (first) select option ---
            # These attributes are server-rendered inside the expander form
            expander = page.locator(
                f"form:has(input[name='product_nr'][value='{supplier_part_no}'])"
            )
            box_option = expander.locator("select.package_type option").first
            data_price = await box_option.get_attribute("data-price")
            data_qty   = await box_option.get_attribute("data-qty")
            log.info(f"Expander data-price={data_price!r}, data-qty={data_qty!r}")

            if not data_price or not data_qty or float(data_price) == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} has no pricing available on hopefix.cz."
                )

            price_raw      = float(data_price)
            price_unit_qty = int(round(float(data_qty) * 100))  # data-qty is in units of 100 pcs

            # --- Stock: table column index 6 "Stock (100 pcs)" — loaded after networkidle ---
            # Columns: [0]DIN [1]IDcode [2]Dim [3]ISO [4]Material [5]YourNr [6]Stock(100pcs) [7]EUR
            stock_value = 0
            cells = row.locator("td")
            if await cells.count() > 6:
                stock_text = (await cells.nth(6).inner_text()).strip()
                log.info(f"Stock cell text: {stock_text!r}")
                m = re.search(r"[\d]+(?:[.,][\d]+)?", stock_text)
                if m:
                    stock_value = int(float(m.group().replace(",", "."))) * 100

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
