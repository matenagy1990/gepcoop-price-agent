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

log = logging.getLogger("mekrs")

LOGIN_URL    = "https://eshop.mekrs.cz/en"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "mekrs_session.json"


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
    """Logged in if the login form is NOT visible."""
    return await page.locator("input[name='username']").count() == 0


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

        async def _do_login():
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            username = os.getenv("SUPPLIER_D_USERNAME", "")
            log.info(f"Logging in as: {username}")
            await page.locator("input[name='username']").fill(username)
            await page.locator("input[name='password']").fill(os.getenv("SUPPLIER_D_PASSWORD", ""))
            await page.locator("[data-testid='login-button']").click()
            await page.wait_for_timeout(3000)
            if await page.locator("input[name='username']").count() > 0:
                raise RuntimeError("Login to eshop.mekrs.cz failed. Please check credentials.")
            log.info(f"Login successful — URL: {page.url}")

        try:
            await emit("Opening eshop.mekrs.cz…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to eshop.mekrs.cz…")
                    await _do_login()
                    _save_cookies(await context.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to eshop.mekrs.cz…")
                await _do_login()
                _save_cookies(await context.cookies())

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
            # Wait for product cards AND for price to be AJAX-rendered
            try:
                await page.wait_for_selector("[data-testid='product-card']", timeout=10000)
            except PlaywrightTimeout:
                raise RuntimeError(f"Part {supplier_part_no} was not found on eshop.mekrs.cz.")
            # Wait for Kč price text to appear in DOM (AJAX-loaded)
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('Kč')",
                    timeout=10000,
                )
                log.info("Kč price text detected in DOM")
            except PlaywrightTimeout:
                log.warning("Kč not found in DOM after 10s — attempting extraction anyway")
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

            # Price and unit qty — find them as a PAIRED set:
            # locate the "/ N pcs" unit element first, then find the Kč price
            # in the same ancestor block (avoids mismatching rows).
            price_str, unit_str, price_elem_html = await page.evaluate("""() => {
                const leaves = Array.from(document.querySelectorAll('*'))
                    .filter(el => el.childElementCount === 0);

                for (const unitEl of leaves) {
                    const ut = unitEl.textContent.trim();
                    if (!/\\/\\s*\\d[\\d,]*\\s*pcs/.test(ut)) continue;

                    // Walk up to 6 levels to find a common ancestor that also
                    // contains a sibling Kč price element
                    let ancestor = unitEl.parentElement;
                    for (let i = 0; i < 6; i++) {
                        if (!ancestor) break;
                        const priceEl = Array.from(ancestor.querySelectorAll('*'))
                            .find(el =>
                                el.childElementCount === 0 &&
                                /[\\d][\\d.,]*\\s*Kč/.test(el.textContent.trim())
                            );
                        if (priceEl) {
                            return [
                                priceEl.textContent.trim(),
                                ut,
                                priceEl.outerHTML,
                            ];
                        }
                        ancestor = ancestor.parentElement;
                    }
                }
                return [null, null, null];
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

            # Parse price: English format — dot=decimal sep, comma=thousands sep
            # "128.96 Kč"    → 128.96   (dot only → decimal)
            # "1,234.56 Kč"  → 1234.56  (comma=thousands, dot=decimal → remove commas)
            # "1,234 Kč"     → 1234     (comma=thousands only → remove commas)
            price_clean = re.sub(r"[^\d.,]", "", price_str)
            if "," in price_clean:
                price_clean = price_clean.replace(",", "")
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
