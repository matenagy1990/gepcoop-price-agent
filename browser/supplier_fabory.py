"""
Playwright scraper for fabory.com (Supplier E)

Session flow:
  1. Try to restore saved session from assets/sessions/fabory_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to search URL — if redirected to /login → session invalid
  3. If session missing / stale / invalid: full login, save new session

Login flow (fallback):
  1. GET /hu/login → accept cookie banner → fill email + password → "Belépés"
  2. Redirects to /hu on success

Search flow:
  4. /hu/search?text={supplier_part_no}
  5. Click first product link
  6. Extract Nettó ár (price), Ár / (unit qty), Készlet (stock)

Price format: "605 Ft" → 605 HUF per unit_qty pieces (unit_qty from "Ár /" column)
Stock format: "Készleten" = in stock, anything else = out of stock
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("fabory")

LOGIN_URL  = "https://www.fabory.com/hu/login"
SEARCH_URL = "https://www.fabory.com/hu/search?text={part_no}"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "fabory_session.json"
_SESSION_MAX_AGE_H = 20


async def _extract_top_variant_price(page) -> tuple[float, int] | None:
    """Read the exact product detail price block at the top of the Fabory variant page."""
    data = await page.evaluate("""() => {
        const block =
            document.querySelector('.product-variant.price-call-triggered.stock-call-triggered') ||
            document.querySelector('.product-variant');
        if (!block) return null;

        const priceText = (block.querySelector('b.price')?.textContent || '').trim();
        const unitText =
            (block.querySelector('[data-product-price-unitquantity]')?.textContent || '').trim() ||
            (block.querySelector('#unitQuantity [data-product-price-unitquantity]')?.textContent || '').trim();
        return { priceText, unitText };
    }""")
    if not data:
        return None

    price_text = (data.get("priceText") or "").strip()
    unit_text = (data.get("unitText") or "").strip()
    if not price_text or not unit_text:
        return None

    price_match = re.search(r"([\d][\d\s\u00a0]*)\s*Ft", price_text)
    unit_match = re.search(r"(\d+)", unit_text)
    if not price_match or not unit_match:
        return None

    price_raw = float(re.sub(r"[\s\u00a0]", "", price_match.group(1)))
    unit_qty = int(unit_match.group(1))
    return price_raw, unit_qty


async def _extract_top_variant_stock(page):
    """Read stock state from the same top variant block that shows the active price."""
    data = await page.evaluate("""() => {
        const block =
            document.querySelector('.product-variant.price-call-triggered.stock-call-triggered') ||
            document.querySelector('.product-variant');
        if (!block) return null;

        const stockEl = block.querySelector('.stock-txt, [data-product-stock-status], .adp-stock');
        const stockText = (stockEl?.textContent || block.querySelector('.adp-stock')?.textContent || '').trim();
        const stockClass = stockEl?.className || '';
        return { stockText, stockClass };
    }""")
    if not data:
        return None

    stock_text = (data.get("stockText") or "").strip()
    stock_class = (data.get("stockClass") or "").strip().lower()
    if not stock_text and not stock_class:
        return None

    if "outofstock" in stock_class or "nincs készleten" in stock_text.lower():
        return 0
    if "instock" in stock_class or "készleten" in stock_text.lower() or "raktáron" in stock_text.lower():
        return "Raktáron"
    return None


async def _extract_price_and_unit_structured(page) -> tuple[float, int] | None:
    """Try a DOM-scoped extraction before falling back to full-body regex."""
    data = await page.evaluate("""() => {
        const leaves = Array.from(document.querySelectorAll('*'))
            .filter(el => el.childElementCount === 0)
            .map(el => el.textContent.trim())
            .filter(Boolean);

        const priceText = leaves.find(t => /\\b[\\d\\s\\u00a0]+\\s*Ft\\b/.test(t));
        const unitText  = leaves.find(t => /ár\\s*\\/\\s*\\d+/i.test(t));
        return { priceText, unitText };
    }""")
    price_text = (data or {}).get("priceText")
    unit_text = (data or {}).get("unitText")
    if not price_text or not unit_text:
        return None

    price_match = re.search(r"([\d][\d\s\u00a0]*)\s*Ft", price_text)
    unit_match = re.search(r"ár\s*/\s*(\d+)", unit_text, re.IGNORECASE)
    if not price_match or not unit_match:
        return None

    price_raw = float(re.sub(r"[\s\u00a0]", "", price_match.group(1)))
    unit_qty = int(unit_match.group(1))
    return price_raw, unit_qty
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
            await emit("Opening fabory.com…")
            search_url = SEARCH_URL.format(part_no=quote(supplier_part_no, safe=""))
            log.info("Fabory search uses supplier_part_no=%r", supplier_part_no)

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
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
                log.info(f"Loaded login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Összes elfogadása").click(timeout=5000)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    log.info("No cookie banner appeared")

                await emit("Logging in to fabory.com…")
                username = os.getenv("SUPPLIER_E_USERNAME", "")
                log.info(f"Filling login form for user: {username}")

                await page.get_by_role("textbox", name="Email cím").fill(username)
                await page.locator("input[placeholder='Jelszó']").fill(os.getenv("SUPPLIER_E_PASSWORD", ""))
                await page.get_by_role("button", name="Belépés").click()

                try:
                    await page.wait_for_url("https://www.fabory.com/hu", timeout=15000)
                    log.info(f"Login successful: {page.url}")
                except PlaywrightTimeout:
                    log.error(f"Login failed — URL: {page.url}")
                    raise RuntimeError("Login to fabory.com failed. Please check credentials.")

                await save_session(context, _SESSION_FILE)

                await emit(f"Searching for {supplier_part_no} on fabory.com…")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

            else:
                await emit(f"Searching for {supplier_part_no} on fabory.com…")

            log.info(f"Search page loaded: {page.url}")
            try:
                await page.wait_for_function(
                    """() => {
                        const hasProductLink = !!document.querySelector("a[href*='/p/']");
                        const txt = (document.body.innerText || '').toLowerCase();
                        const hasNoResults = txt.includes('0 találat') || txt.includes('no results');
                        return hasProductLink || hasNoResults;
                    }""",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                log.warning("No explicit search result marker appeared after 8s — continuing anyway")

            body_text = await page.locator("body").inner_text()
            if "0 találat" in body_text or "no results" in body_text.lower():
                raise RuntimeError(MSG_NOT_FOUND)

            if "/search" in page.url:
                log.info("On search results page, clicking first product link")
                try:
                    await page.locator("a[href*='/p/']").first.click(timeout=8000)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_function(
                        "() => document.body.innerText.includes('Ft') || document.body.innerText.includes('Készleten') || document.body.innerText.includes('Nincs készleten')",
                        timeout=10000,
                    )
                    log.info(f"Product page: {page.url}")
                except PlaywrightTimeout:
                    # Search returned results, but the product page/price did not load.
                    raise RuntimeError(MSG_NOT_PRICED)

            try:
                await page.wait_for_function(
                    """() => {
                        const price = document.querySelector(".product-variant b.price");
                        const unit = document.querySelector(".product-variant [data-product-price-unitquantity]");
                        return !!price && !!unit &&
                               !!(price.textContent || '').trim() &&
                               !!(unit.textContent || '').trim();
                    }""",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                log.warning("Fabory top variant pricing block did not fully load within 10s")
            await emit("Reading price and stock from fabory.com…")

            body_text = await page.locator("body").inner_text()
            log.info(f"Page URL: {page.url}")

            top_variant = await _extract_top_variant_price(page)
            if top_variant:
                price_raw, unit_qty = top_variant
                log.info("Price extracted from Fabory top variant block")
            else:
                structured = await _extract_price_and_unit_structured(page)
                if structured:
                    price_raw, unit_qty = structured
                    log.info("Price extracted via structured DOM scan")
                else:
                    price_match = re.search(
                        r"([\d][\d\s\u00a0]*)\s*Ft\s*/\s*ár\s*/\s*(\d+)",
                        body_text,
                    )
                    if not price_match:
                        # Product page is open, but no usable price is shown.
                        raise RuntimeError(MSG_NOT_PRICED)
                    price_raw = float(re.sub(r"[\s\u00a0]", "", price_match.group(1)))
                    unit_qty = int(price_match.group(2))
                    log.info("Price extracted via body-text fallback")
            log.info(f"Price: {price_raw} Ft / {unit_qty} db")

            stock_value = await _extract_top_variant_stock(page)
            if stock_value is None:
                log.warning("Fabory top variant stock marker not found — falling back to page-wide stock text")
                if await page.get_by_text("Nincs készleten", exact=False).count():
                    stock_value = 0
                elif await page.get_by_text("Készleten", exact=False).count() or await page.get_by_text("Raktáron", exact=False).count():
                    stock_value = "Raktáron"
                else:
                    stock_value = None
            log.info(f"Stock: {stock_value}")

            return {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   unit_qty,
                "currency":         "HUF",
                "unit":             "db",
                "stock":            stock_value,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during fabory.com scrape: {exc}")
            raise RuntimeError(f"fabory.com scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
