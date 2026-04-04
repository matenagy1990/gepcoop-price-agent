"""
Playwright scraper for eshop.mekrs.cz

Session flow:
  1. Try to restore saved session from assets/sessions/mekrs_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to /en — check if login form (input[name='username']) is absent → logged in
  3. If session missing / stale / invalid: full login, save new session

Login flow (fallback):
  1. GET /en → fill input[name='username'] + input[name='password']
     → click [data-testid='login-button']
  2. Login form disappears on success

Search flow:
  4. Type part number into the main search box
     (input[placeholder='Search by name, code, DIN'])
  5. Wait for autocomplete dropdown → click "Show all results"
  6. Results page: /en/products?nazev={part_no}&onStock=false

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
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh

load_dotenv()

log = logging.getLogger("mekrs")

HOME_URL = "https://eshop.mekrs.cz/en"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "mekrs_session.json"
_SESSION_MAX_AGE_H = 20


def _parse_stock(s: str) -> int:
    """Extract stock number from 'In stock 1,653,361 pcs' → 1653361"""
    if not s:
        return 0
    digit_groups = re.findall(r"\d+", s)
    return int("".join(digit_groups)) if digit_groups else 0


def _parse_czk_price(price_str: str) -> float:
    """Parse CZK price from either '50.63 Kč' or '50,63 Kč'."""
    clean = re.sub(r"[^\d.,]", "", price_str).strip()
    if not clean:
        raise ValueError("Empty CZK price string")
    if "," in clean and "." in clean:
        clean = clean.replace(",", "")
    elif "," in clean:
        clean = clean.replace(",", ".")
    return float(clean)


async def _is_search_visible(page) -> bool:
    return await page.locator("input[placeholder='Search by name, code, DIN']").first.is_visible()


async def _is_login_visible(page) -> bool:
    return await page.locator("input[name='username']").first.is_visible()


async def _wait_for_auth_state(page, timeout_ms: int = 12000) -> str:
    """Return 'search', 'login', or 'unknown' based on the visible post-login UI."""
    try:
        await page.wait_for_function(
            """() => {
                const search = document.querySelector("input[placeholder='Search by name, code, DIN']");
                const login = document.querySelector("input[name='username']");
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                };
                return visible(search) || visible(login);
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeout:
        return "unknown"

    if await _is_search_visible(page):
        return "search"
    if await _is_login_visible(page):
        return "login"
    return "unknown"


async def _extract_price_block(container):
    return await container.evaluate("""(root) => {
        const leaves = Array.from(root.querySelectorAll('*'))
            .filter(el => el.childElementCount === 0);

        for (const unitEl of leaves) {
            const ut = unitEl.textContent.trim();
            if (!/\\/\\s*\\d[\\d,]*\\s*pcs/.test(ut)) continue;

            let ancestor = unitEl.parentElement;
            for (let i = 0; i < 8; i++) {
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

        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            await emit("Opening eshop.mekrs.cz…")

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                state = await _wait_for_auth_state(page, timeout_ms=8000)
                log.info(f"After session restore, URL: {page.url}")

                if state == "login":
                    log.warning("Session invalid (login form visible) — falling back to full login")
                    use_session = False
                    invalidate_session(_SESSION_FILE)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                    page.on("dialog", handle_dialog)
                elif state == "search":
                    log.info("Session valid — login skipped")
                else:
                    log.warning("Session restore ended in unknown state — retrying home page before login fallback")
                    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                    state = await _wait_for_auth_state(page, timeout_ms=8000)
                    if state != "search":
                        use_session = False
                        invalidate_session(_SESSION_FILE)
                        await browser.close()
                        browser  = await pw.chromium.launch(headless=True)
                        context  = await browser.new_context()
                        page     = await context.new_page()
                        page.on("dialog", handle_dialog)
                    else:
                        log.info("Session valid after retry — login skipped")

            # ── Step 2: full login (if needed) ───────────────────────────────
            if not use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await page.wait_for_selector("input[name='username']", timeout=8000)
                log.info(f"Loaded: {page.url}")

                await emit("Logging in to eshop.mekrs.cz…")
                username = os.getenv("SUPPLIER_D_USERNAME", "")
                log.info(f"Logging in as: {username}")

                await page.locator("input[name='username']").fill(username)
                await page.locator("input[name='password']").fill(os.getenv("SUPPLIER_D_PASSWORD", ""))
                await page.locator("[data-testid='login-button']").click()
                state = await _wait_for_auth_state(page, timeout_ms=12000)
                if state == "unknown":
                    log.warning("Login did not resolve to search/login state — retrying homepage")
                    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                    state = await _wait_for_auth_state(page, timeout_ms=8000)

                if state == "login":
                    raise RuntimeError("Login to eshop.mekrs.cz failed. Please check credentials.")
                if state != "search":
                    raise RuntimeError(
                        "eshop.mekrs.cz login completed, but the search page did not become available."
                    )

                log.info(f"Login successful — URL: {page.url}")
                await save_session(context, _SESSION_FILE)

            # ── Step 3: search via autocomplete ──────────────────────────────
            await emit(f"Searching for part {supplier_part_no} on eshop.mekrs.cz…")
            search_inp = page.locator("input[placeholder='Search by name, code, DIN']").first
            await search_inp.wait_for(state="visible", timeout=8000)
            await search_inp.click()
            await search_inp.fill("")
            try:
                await page.wait_for_function(
                    "() => { const el = document.querySelector(\"input[placeholder='Search by name, code, DIN']\"); return !!el && el.value === ''; }",
                    timeout=3000,
                )
            except PlaywrightTimeout:
                log.warning("Mekrs search box did not clear via fill(''); trying select-all fallback")
                await search_inp.press("Control+A")
                await search_inp.press("Backspace")
            await search_inp.type(supplier_part_no, delay=50)
            log.info(f"Typed '{supplier_part_no}' into search box, waiting for autocomplete…")
            try:
                await page.wait_for_function(
                    """(partNo) => {
                        const items = Array.from(document.querySelectorAll('li, [role="option"], a, button'));
                        return items.some(el => (el.textContent || '').includes(partNo)) ||
                               items.some(el => (el.textContent || '').includes('Show all results'));
                    }""",
                    arg=supplier_part_no,
                    timeout=8000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(autocomplete returned no results)."
                )

            exact_suggestion = page.locator("li, [role='option'], a, button").filter(
                has_text=supplier_part_no
            ).first
            if await exact_suggestion.count():
                await exact_suggestion.click()
                log.info("Clicked exact Mekrs autocomplete suggestion")
            else:
                show_all = page.locator("text=Show all results").first
                show_all_count = await show_all.count()
                if show_all_count == 0:
                    raise RuntimeError(
                        f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                        "(autocomplete returned no results)."
                    )
                await show_all.click()
                log.info("Clicked Mekrs 'Show all results' fallback")

            try:
                await page.wait_for_function(
                    """(partNo) => {
                        return !!document.querySelector("[data-testid='product-card']") ||
                               document.body.innerText.includes(partNo) ||
                               document.body.innerText.includes('Kč');
                    }""",
                    arg=supplier_part_no,
                    timeout=10000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(f"Part {supplier_part_no} was not found on eshop.mekrs.cz.")
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('Kč')",
                    timeout=10000,
                )
                log.info("Kč price text detected in DOM")
            except PlaywrightTimeout:
                log.warning("Kč not found in DOM after 10s — attempting extraction anyway")
            log.info(f"Results page loaded: {page.url}")

            cards = page.locator("[data-testid='product-card']")
            card_count = await cards.count()
            log.info(f"Product cards on results page: {card_count}")

            # ── Step 4: extract data ──────────────────────────────────────────
            await emit("Reading price and stock from eshop.mekrs.cz…")
            if card_count > 0:
                target_card = cards.filter(has_text=supplier_part_no).first
                if await target_card.count() == 0:
                    raise RuntimeError(
                        f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                        "(results page did not contain an exact matching product card)."
                    )
            else:
                target_card = page.locator("main").first
                if await target_card.count() == 0:
                    target_card = page.locator("body").first
                log.info("No Mekrs product-card found; falling back to detail-page extraction")

            try:
                stock_str = await target_card.locator(
                    "div.text-sm.font-medium.text-primaryGreen"
                ).inner_text(timeout=5000)
                stock_str = stock_str.strip()
            except PlaywrightTimeout:
                stock_str = ""
                log.warning("Stock element not found — assuming out of stock")

            price_str, unit_str, price_elem_html = await _extract_price_block(target_card)

            log.info(f"Price element HTML: {price_elem_html}")
            log.info(f"Raw — price: '{price_str}', unit: '{unit_str}', stock: '{stock_str}'")

            if not price_str:
                card_snippet = await target_card.evaluate(
                    "(card) => card.innerHTML.slice(0, 3000)"
                )
                log.error(f"Price not found in target card — card HTML snippet:\n{card_snippet}")
                raise RuntimeError(
                    f"Part {supplier_part_no} has no visible price on eshop.mekrs.cz."
                )

            price_raw = _parse_czk_price(price_str)

            qty_match = re.search(r"([\d,]+)\s*pcs", unit_str)
            if qty_match:
                price_unit_qty = int(qty_match.group(1).replace(",", ""))
            else:
                price_unit_qty = 1
                log.warning(f"Could not parse unit qty from '{unit_str}', assuming 1")

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
