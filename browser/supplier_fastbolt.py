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

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("fastbolt")

LOGIN_URL  = "https://fbonline.fastbolt.com/login"
HOME_URL = "https://fbonline.fastbolt.com/"
MATRIX_URL = "https://fbonline.fastbolt.com/matrix/{part_no}"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "fastbolt_session.json"
SESSION_MAX_AGE_HOURS = 20


async def _find_matrix_cell(page, supplier_part_no: str):
    candidates = [
        page.locator(f".matrix [title='{supplier_part_no}']"),
        page.locator(f"table [title='{supplier_part_no}']"),
        page.locator(f"[title='{supplier_part_no}']"),
    ]
    for locator in candidates:
        count = await locator.count()
        if not count:
            continue
        for idx in range(count):
            cell = locator.nth(idx)
            try:
                if await cell.is_visible():
                    return cell, count
            except Exception:
                continue
    return None, 0


async def _find_enquiry_item(page, supplier_part_no: str):
    direct = page.locator(f".item.enquiry-item:has(input#enquiry-item-{supplier_part_no})").first
    if await direct.count():
        return direct

    fallback = page.locator(
        f".item.enquiry-item:has-text('{supplier_part_no}'), "
        f".item.enquiry-item:has-text('FB Mat.-No.')"
    )
    count = await fallback.count()
    for idx in range(count):
        item = fallback.nth(idx)
        try:
            text = await item.inner_text()
        except Exception:
            continue
        if supplier_part_no in text:
            return item
    return None


async def _login_or_restore(pw, emit: Callable):
    """Launch a browser and return (browser, context, page) on a logged-in
    fastbolt context. Shared by both entrypoints."""
    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, SESSION_MAX_AGE_HOURS))

    browser = await pw.chromium.launch(headless=True)
    if use_session:
        try:
            context = await browser.new_context(storage_state=session["state"])
            log.info("Restored saved session (age < 20 h)")
        except Exception as exc:
            log.warning(f"Could not restore saved session: {exc}")
            invalidate_session(SESSION_FILE)
            use_session = False
            context = await browser.new_context()
    else:
        if session:
            log.info("Session stale (> 20 h) — proactive re-login")
            invalidate_session(SESSION_FILE)
        context = await browser.new_context()
    page = await context.new_page()

    await emit("Opening fbonline.fastbolt.com…")

    if use_session:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
        if "/login" in page.url:
            log.warning("Saved session is no longer valid — performing fresh login")
            invalidate_session(SESSION_FILE)
            await context.close()
            context = await browser.new_context()
            page = await context.new_page()
            use_session = False
        else:
            log.info("Session valid — login skipped")

    if not use_session:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        log.info(f"Loaded login page: {page.url}")

        await emit("Logging in to fbonline.fastbolt.com…")
        shortname = os.getenv("SUPPLIER_H_SHORTNAME", "")
        username  = os.getenv("SUPPLIER_H_USERNAME", "")
        password  = os.getenv("SUPPLIER_H_PASSWORD", "")
        log.info(f"Logging in — shortname: {shortname}, user: {username}")

        await page.get_by_role("searchbox", name="Shortname:").fill(shortname)
        await page.get_by_role("searchbox", name="Loginname:").fill(username)
        await page.get_by_role("textbox", name="Password:").fill(password)
        await page.get_by_role("button", name="Sign in").click()

        try:
            await page.wait_for_function("() => !location.pathname.includes('/login')", timeout=12000)
        except PlaywrightTimeout:
            raise RuntimeError("Login to fbonline.fastbolt.com failed. Please check credentials.")
        log.info(f"Login successful: {page.url}")
        try:
            await save_session(context, SESSION_FILE)
        except Exception as exc:
            log.warning(f"Could not persist session: {exc}")

    return browser, context, page


async def _search_and_parse(page, supplier_part_no: str, emit: Callable) -> dict:
    """Look up one part on a logged-in fastbolt session via the matrix + enquiry
    panel, and parse price + stock."""
    # Navigate to the product matrix page
    await emit(f"Searching for {supplier_part_no} on fastbolt…")
    matrix_url = MATRIX_URL.format(part_no=supplier_part_no)
    await page.goto(matrix_url, wait_until="domcontentloaded", timeout=20000)
    log.info(f"Matrix page: {page.url}")

    # If redirected away, the part number does not exist on fastbolt
    if f"/matrix/{supplier_part_no}" not in page.url:
        raise RuntimeError(MSG_NOT_FOUND)

    # Click the specific cell in the matrix table to add it to the enquiry panel
    matrix_cell, match_count = await _find_matrix_cell(page, supplier_part_no)
    if not matrix_cell:
        raise RuntimeError(MSG_NOT_FOUND)
    log.info("Fastbolt matrix candidates for %s: %s", supplier_part_no, match_count)
    try:
        await matrix_cell.click()
        log.info(f"Clicked visible matrix cell for {supplier_part_no}")
    except PlaywrightTimeout:
        # The matrix cell exists but could not be opened to load a price.
        raise RuntimeError(MSG_NOT_PRICED)

    # Wait for the enquiry panel to register the selected part.
    qty_selector = f"input#enquiry-item-{supplier_part_no}"
    try:
        await page.wait_for_function(
            """partNo => {
                const byInput = document.querySelector(`input#enquiry-item-${partNo}`);
                if (byInput) return true;
                const items = [...document.querySelectorAll('.item.enquiry-item')];
                return items.some(item => item.innerText.includes(partNo));
            }""",
            arg=supplier_part_no,
            timeout=12000,
        )
        log.info("Enquiry panel registered %s", supplier_part_no)
    except PlaywrightTimeout:
        panel_snapshot = await page.evaluate(
            """() => ({
                enquiryInputs: [...document.querySelectorAll('input[id^="enquiry-item-"]')].map(el => el.id),
                enquiryItems: [...document.querySelectorAll('.item.enquiry-item')].map(el => el.innerText.slice(0, 250))
            })"""
        )
        log.warning("Fastbolt enquiry panel snapshot: %s", panel_snapshot)
        raise RuntimeError(MSG_NOT_PRICED)

    await emit("Reading price and stock from fastbolt…")

    item_locator = await _find_enquiry_item(page, supplier_part_no)
    if not item_locator:
        raise RuntimeError(MSG_NOT_PRICED)

    # Type "1" into the quantity field and click the checkmark to trigger price load
    qty_input = item_locator.locator(qty_selector)
    if not await qty_input.count():
        qty_input = item_locator.locator("input[id^='enquiry-item-']").first
    await qty_input.wait_for(timeout=8000)
    await qty_input.fill("1")
    log.info("Filled quantity with 1")

    # Click the checkmark inside the enquiry item row for this specific part
    await item_locator.locator("a.btn-change-amount").click()
    log.info("Clicked checkmark to confirm quantity")

    # Wait for the AJAX price update — span.text-red > b must contain EUR
    try:
        await item_locator.locator(".current-amount span.text-red").first.wait_for(timeout=10000)
        await page.wait_for_function(
            """partNo => {
                const item = document.querySelector(`.item.enquiry-item:has(input#enquiry-item-${partNo})`)
                  || [...document.querySelectorAll('.item.enquiry-item')].find(el => el.innerText.includes(partNo));
                if (!item) return false;
                const price = item.querySelector('.current-amount span.text-red');
                return price && price.innerText.includes('EUR');
            }""",
            arg=supplier_part_no,
            timeout=10000,
        )
        log.info("Price loaded in enquiry panel")
    except PlaywrightTimeout:
        log.warning("Price did not appear after 10s — attempting extraction anyway")

    # Extract price and unit qty — scope to the specific enquiry item for this part
    price_span = item_locator.locator(".current-amount span.text-red").first
    price_data = (await price_span.inner_text()).strip() if await price_span.count() else None

    log.info(f"Raw price text: {price_data!r}")

    if not price_data or "EUR" not in price_data:
        body = await page.locator("body").inner_text()
        log.error(f"Price not found. Body snippet: {body[:500]}")
        # Product is in the enquiry panel, but no usable price is shown.
        raise RuntimeError(MSG_NOT_PRICED)

    # Parse: "3.10 EUR\n/ 100\n= 7.75 EUR"
    # Price value
    price_match = re.search(r"([\d.,]+)\s*EUR", price_data)
    if not price_match:
        raise RuntimeError(MSG_NOT_PRICED)
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
    stock_bar = item_locator.locator(".progress-bar-success").first
    stock_text = (await stock_bar.inner_text()).strip() if await stock_bar.count() else ""
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


# ── Single lookup (price agent) ──────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser, context, page = await _login_or_restore(pw, emit)
        try:
            return await _search_and_parse(page, supplier_part_no, emit)
        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during fastbolt scrape: {exc}")
            raise RuntimeError(f"fastbolt scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")


# ── Batch lookup (batch agent) ───────────────────────────────────────────────

async def fetch_prices(
    part_nos: list[str],
    on_progress: Callable | None = None,
    on_item: Callable | None = None,
) -> list[dict]:
    """Look up several parts in ONE fastbolt session (login once, reuse browser)."""
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    results: list[dict] = []
    if not part_nos:
        return results

    total = len(part_nos)

    async with async_playwright() as pw:
        browser, context, page = await _login_or_restore(pw, emit)
        try:
            for i, pn in enumerate(part_nos):
                try:
                    r = await _search_and_parse(page, pn, emit)
                    results.append(r)
                    if on_item:
                        await on_item(i, total, pn, r, None)
                except RuntimeError as exc:
                    msg = str(exc)
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
                except Exception as exc:
                    log.exception(f"Unexpected error during fastbolt scrape ({pn}): {exc}")
                    msg = f"fastbolt scrape failed: {exc}"
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
        finally:
            await browser.close()
            log.info("Browser closed")

    return results


def _parse_stock(s: str) -> int:
    """Extract integer from stock text, e.g. '1,250' → 1250, '250' → 250"""
    if not s:
        return 0
    cleaned = re.sub(r"[^\d]", "", s)
    return int(cleaned) if cleaned else 0
