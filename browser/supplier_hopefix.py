"""
Playwright scraper for hopefix.cz (Supplier G)

Session flow:
  1. Try to restore saved session from assets/sessions/hopefix_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to /en — if redirected to /en/login → session invalid
  3. If session missing / stale / invalid: full login, save new session

Login flow (fallback):
  1. GET /en/login → accept cookie banner → fill E-mail + Password → "Login"
  2. Redirects to /en/products on success

Search flow:
  4. Type part number into the autocomplete search box (#search_input)
  5. Wait for the jQuery UI autocomplete dropdown (#ui-id-1) to appear
  6. Click the suggestion that matches the part number
     → navigates to /en/products/{slug}#{part_no}
  7. Find the table row whose text contains the part number
  8. Extract EUR price from the cell containing '€' and stock from the cell before it

Currency: EUR
Price column: "EUR/100 pcs" → price_unit_qty = 100
Stock column: "Stock (100 pcs)" — raw value stored as int
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

log = logging.getLogger("hopefix")

LOGIN_URL  = "https://www.hopefix.cz/en/login"
HOME_URL   = "https://www.hopefix.cz/en"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "hopefix_session.json"
_SESSION_MAX_AGE_H = 20


async def _extract_stock_from_row(row) -> int:
    """Prefer header-based stock lookup, fall back to the historic fixed index."""
    stock_text = await row.evaluate("""(el) => {
        const cells = Array.from(el.querySelectorAll('td'));
        const table = el.closest('table');
        if (!table) return '';

        const headers = Array.from(table.querySelectorAll('thead th'))
            .map(th => th.textContent.trim().toLowerCase());
        const idx = headers.findIndex(h => h.includes('stock'));
        if (idx >= 0 && cells[idx]) return cells[idx].textContent.trim();
        return '';
    }""")

    if not stock_text:
        cells = row.locator("td")
        if await cells.count() > 6:
            stock_text = (await cells.nth(6).inner_text()).strip()

    if not stock_text:
        return 0

    log.info(f"Stock cell text: {stock_text!r}")
    m = re.search(r"[\d]+(?:[.,][\d]+)?", stock_text)
    if not m:
        return 0
    return int(float(m.group().replace(",", "."))) * 100
# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session = load_session(_SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, _SESSION_MAX_AGE_H))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        if use_session:
            log.info("Restoring saved session (age < 20 h)")
            try:
                context = await browser.new_context(storage_state=session["state"])
            except Exception as exc:
                log.warning(f"Could not restore session state: {exc}")
                use_session = False
                invalidate_session(_SESSION_FILE)
                context = await browser.new_context()
        else:
            if session:
                log.info("Session stale (> 20 h) — proactive re-login")
                invalidate_session(_SESSION_FILE)
            context = await browser.new_context()

        page = await context.new_page()

        try:
            await emit("Opening hopefix.cz…")

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                log.info(f"After session restore, URL: {page.url}")

                if "/login" in page.url:
                    log.warning("Session invalid — falling back to full login")
                    use_session = False
                    invalidate_session(_SESSION_FILE)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                else:
                    log.info("Session valid — login skipped")

            # ── Step 2: full login (if needed) ───────────────────────────────
            if not use_session:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                log.info(f"Login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Vše přijmout").click(timeout=5000)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    try:
                        await page.get_by_role("button", name="Accept all").click(timeout=3000)
                    except PlaywrightTimeout:
                        log.info("No cookie banner")

                await emit("Logging in to hopefix.cz…")
                username = os.getenv("SUPPLIER_G_USERNAME", "")
                password = os.getenv("SUPPLIER_G_PASSWORD", "")
                log.info(f"Logging in as: {username}")

                await page.get_by_role("textbox", name="E-mail").fill(username)
                await page.get_by_role("textbox", name="Password").fill(password)
                await page.get_by_role("button", name="Login").click()
                try:
                    await page.wait_for_function(
                        "() => !location.pathname.includes('/login') && !!document.querySelector('#search_input')",
                        timeout=12000,
                    )
                except PlaywrightTimeout:
                    pass

                if "/login" in page.url:
                    raise RuntimeError("Login to hopefix.cz failed. Please check credentials.")
                log.info(f"Login successful: {page.url}")

                await save_session(context, _SESSION_FILE)

            # ── Step 3: search via autocomplete ──────────────────────────────
            await emit(f"Searching for {supplier_part_no} on hopefix.cz…")
            search_box = page.locator("#search_input")
            await search_box.wait_for(state="visible", timeout=8000)
            await search_box.click()
            await search_box.fill("")
            try:
                await page.wait_for_function(
                    "() => { const el = document.querySelector('#search_input'); return !!el && el.value === ''; }",
                    timeout=3000,
                )
            except PlaywrightTimeout:
                log.warning("Search box did not clear via fill(''); trying select-all fallback")
                await search_box.press("Control+A")
                await search_box.press("Backspace")
                await page.wait_for_function(
                    "() => { const el = document.querySelector('#search_input'); return !!el && el.value === ''; }",
                    timeout=3000,
                )
            await search_box.type(supplier_part_no, delay=60)
            await page.wait_for_function(
                "(expected) => { const el = document.querySelector('#search_input'); return !!el && el.value === expected; }",
                arg=supplier_part_no,
                timeout=3000,
            )
            log.info("Typed part number, waiting for autocomplete…")

            try:
                await page.wait_for_selector(
                    f"#ui-id-1 li:has-text('{supplier_part_no}')",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(MSG_NOT_FOUND)

            suggestion = page.locator("#ui-id-1 li").filter(has_text=supplier_part_no).first
            await suggestion.click(timeout=12000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                log.warning("networkidle timeout — continuing anyway")
            try:
                await page.locator("tr").filter(has_text=supplier_part_no).first.wait_for(timeout=8000)
            except PlaywrightTimeout:
                raise RuntimeError(MSG_NOT_FOUND)
            log.info(f"Product page: {page.url}")

            await emit("Reading price and stock from hopefix.cz…")

            row = page.locator("tr").filter(has_text=supplier_part_no).first
            if await row.count() == 0:
                raise RuntimeError(MSG_NOT_FOUND)

            toggle = row.locator(".toggle-expander")
            if await toggle.count() == 0:
                # The product row is present, but no pricing panel is offered.
                raise RuntimeError(MSG_NOT_PRICED)

            await toggle.click()

            try:
                await page.wait_for_function(
                    """(partNo) => {
                        const forms = Array.from(document.querySelectorAll('form'));
                        return forms.some((form) => {
                            const input = form.querySelector('input[name="product_nr"]');
                            const select = form.querySelector('select.package_type');
                            if (!input || !select || input.value !== partNo) return false;
                            const style = window.getComputedStyle(select);
                            const rect = select.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' &&
                                   rect.width > 0 && rect.height > 0;
                        });
                    }""",
                    arg=supplier_part_no,
                    timeout=8000,
                )
            except PlaywrightTimeout:
                log.warning(
                    "Visible pricing panel did not appear on hopefix.cz; trying hidden form fallback"
                )

            forms = page.locator(f"form:has(input[name='product_nr'][value='{supplier_part_no}'])")
            expander = None
            for idx in range(await forms.count()):
                candidate = forms.nth(idx)
                select = candidate.locator("select.package_type")
                if await select.count() and await select.first.is_visible():
                    expander = candidate
                    break

            if expander is None:
                for idx in range(await forms.count()):
                    candidate = forms.nth(idx)
                    if await candidate.locator("select.package_type option").count():
                        expander = candidate
                        log.warning(
                            "Pricing panel stayed hidden on hopefix.cz; falling back to matching hidden form"
                        )
                        break

            if expander is None:
                # The product row is present, but the pricing panel did not open.
                raise RuntimeError(MSG_NOT_PRICED)

            box_option = expander.locator("select.package_type option").first
            data_price = await box_option.get_attribute("data-price")
            data_qty   = await box_option.get_attribute("data-qty")
            log.info(f"Expander data-price={data_price!r}, data-qty={data_qty!r}")

            if not data_price or not data_qty or float(data_price) == 0:
                # The pricing panel opened, but no usable price is set.
                raise RuntimeError(MSG_NOT_PRICED)

            price_raw      = float(data_price)
            price_unit_qty = int(round(float(data_qty) * 100))

            stock_value = await _extract_stock_from_row(row)

            log.info(f"Parsed — {price_raw} EUR / {price_unit_qty} pcs, stock: {stock_value}")

            # The autocomplete navigation lands on the exact product page
            # (…/en/products/<slug>#<part>). The slug isn't derivable from the part
            # number, but we are already on that page — hand the URL back so
            # "Tovább a honlapra" opens the product directly (the listing/home is
            # /en/products without a slug, so require a slug segment).
            product_url = page.url if "/en/products/" in page.url else None
            log.info(f"Product detail URL: {product_url}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "EUR",
                "unit":             "db",
                "stock":            stock_value,
                "product_url":      product_url,
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
