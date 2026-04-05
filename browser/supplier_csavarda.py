"""
Playwright scraper for csavarda.hu

Session flow:
  1. Try to restore saved session from assets/sessions/csavarda_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to search URL and verify session:
     - Redirected to /bejelentkezes → session invalid
     - Redirected to /telephely-valasztasa → logged in but location needs re-selection
     - Otherwise → session valid, skip login
  3. If session missing / stale / invalid: full login, save new session with timestamp

Login flow (fallback):
  1. GET /bejelentkezes → fill #email + #password → submit
  2. Redirected to /telephely-valasztasa → select Budapest (/pest)
  3. Save session here (location is now selected in cookies)

Search flow:
  4. /pest/kereso?search={supplier_part_no}
  5. Click first product link → side drawer opens
  6. Extract Nettó egységár, Készlet (Budapest + Vecsés)
"""

import asyncio
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

log = logging.getLogger("csavarda")

LOGIN_URL  = "https://csavarda.hu/bejelentkezes"
SEARCH_URL = "https://csavarda.hu/pest/kereso?search={part_no}"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "csavarda_session.json"
_SESSION_MAX_AGE_H = 20

_JS_NEXT_SIBLING = """
(labelText) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.trim() === labelText)
            return el.nextElementSibling?.textContent?.trim() ?? null;
    }
    return null;
}
"""

_JS_CONTAINS = """
(substr) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.includes(substr))
            return el.textContent.trim();
    }
    return null;
}
"""


async def _find_exact_product_link(page, supplier_part_no: str):
    links = page.locator("a[href*='/pest/termek/']")
    count = await links.count()
    for idx in range(count):
        link = links.nth(idx)
        try:
            link_text = await link.inner_text()
        except Exception:
            link_text = ""
        if supplier_part_no in link_text:
            return link
        try:
            row_text = await link.evaluate(
                """el => {
                    const parent =
                        el.closest('li, article, tr, .product-card, .product-item, .card, .grid-item, .row') ||
                        el.parentElement;
                    return (parent?.innerText || '').trim();
                }"""
            )
        except Exception:
            row_text = ""
        if supplier_part_no in row_text:
            return link
    return None
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
        page.on("dialog", lambda d: (log.info(f"Dialog dismissed: {d.message}"), d.accept()))

        try:
            await emit("Opening csavarda.hu…")
            search_url = SEARCH_URL.format(part_no=supplier_part_no)

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                log.info(f"After session restore, URL: {page.url}")

                if "/bejelentkezes" in page.url:
                    log.warning("Session invalid — falling back to full login")
                    use_session = False
                    invalidate_session(_SESSION_FILE)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                    page.on("dialog", lambda d: (log.info(f"Dialog dismissed: {d.message}"), d.accept()))

                elif "/telephely-valasztasa" in page.url:
                    # Logged in but location not stored — select Budapest and continue
                    log.info("Session valid but location not set — selecting Budapest")
                    await page.get_by_role("link", name=re.compile("Budapesti telephely")).click()
                    await page.wait_for_url("**/pest", timeout=10000)
                    await save_session(context, _SESSION_FILE)
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

                else:
                    # URL check passed, but csavarda serves the search page to
                    # guests too — prices are only visible to authenticated users.
                    # Extra check: wait briefly, then verify "Ft" is in the body
                    # (prices appear only when logged in).
                    await page.wait_for_timeout(1500)
                    body_snapshot = await page.locator("body").inner_text()
                    zero_results = "0 találat" in body_snapshot
                    prices_visible = "Ft" in body_snapshot
                    if not prices_visible and not zero_results:
                        log.warning("Session expired server-side (prices not visible) — falling back to full login")
                        use_session = False
                        invalidate_session(_SESSION_FILE)
                        await browser.close()
                        browser  = await pw.chromium.launch(headless=True)
                        context  = await browser.new_context()
                        page     = await context.new_page()
                        page.on("dialog", lambda d: (log.info(f"Dialog dismissed: {d.message}"), d.accept()))
                    else:
                        log.info("Session valid — login skipped")

            # ── Step 2: full login (if needed) ───────────────────────────────
            if not use_session:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                log.info(f"Loaded login page: {page.url}")

                try:
                    await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
                    await page.wait_for_timeout(800)
                    log.info("Cookie banner accepted")
                except PlaywrightTimeout:
                    log.info("No cookie banner appeared")

                await emit("Logging in to csavarda.hu…")
                username = os.getenv("SUPPLIER_A_USERNAME", "")
                log.info(f"Filling login form for user: {username}")
                await page.locator("#email").fill(username)
                await page.locator("#password").fill(os.getenv("SUPPLIER_A_PASSWORD", ""))
                filled_email = await page.locator("#email").input_value()
                filled_pass  = await page.locator("#password").input_value()
                log.info(f"Form filled — email: {filled_email}, password length: {len(filled_pass)}")

                await page.locator("button:has-text('Bejelentkezés')").click()
                log.info("Login button clicked, waiting for redirect…")

                try:
                    await page.wait_for_url("**/telephely-valasztasa", timeout=12000)
                    log.info(f"Login successful, redirected to: {page.url}")
                except PlaywrightTimeout:
                    current_url = page.url
                    log.error(f"Login failed — still on: {current_url}")
                    body_text = await page.locator("body").inner_text()
                    for line in body_text.splitlines():
                        line = line.strip()
                        if line and any(w in line.lower() for w in ["hiba", "error", "sikertelen", "érvénytelen", "helytelen"]):
                            log.error(f"Page error text: {line}")
                    raise RuntimeError("Login to csavarda.hu failed. Please check credentials.")

                log.info("Selecting Budapest location…")
                await page.get_by_role("link", name=re.compile("Budapesti telephely")).click()
                await page.wait_for_url("**/pest", timeout=10000)
                log.info(f"Location selected, now at: {page.url}")

                await save_session(context, _SESSION_FILE)

                await emit(f"Searching for part {supplier_part_no} on csavarda.hu…")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

            else:
                await emit(f"Searching for part {supplier_part_no} on csavarda.hu…")

            # ── Step 3: wait for results ──────────────────────────────────────
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.ensure_future(page.wait_for_selector("a[href*='/pest/termek/']", timeout=15000)),
                        asyncio.ensure_future(page.wait_for_selector("text=0 találat", timeout=15000)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
            except Exception:
                pass

            log.info(f"Search page loaded: {page.url}")

            zero_results = await page.locator("text=0 találat").count()
            if zero_results > 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on csavarda.hu.")

            product_links = await page.locator("a[href*='/pest/termek/']").count()
            log.info(f"Product links found: {product_links}")
            if product_links == 0:
                raise RuntimeError(f"No product links found for part {supplier_part_no} on csavarda.hu.")

            # ── Step 4: open exact product drawer ────────────────────────────
            exact_link = await _find_exact_product_link(page, supplier_part_no)
            if not exact_link:
                raise RuntimeError(
                    f"Part {supplier_part_no} was listed on csavarda.hu, but no exact product link could be identified."
                )

            link_text = (await exact_link.inner_text()).strip()
            log.info("Clicking exact Csavarda result for %s (link text=%r)", supplier_part_no, link_text)
            await exact_link.click()

            try:
                await page.wait_for_selector("text=Nettó egységár:", timeout=15000)
                log.info("Product drawer opened — 'Nettó egységár:' label found")
            except PlaywrightTimeout:
                raise RuntimeError("csavarda.hu page layout may have changed — price selector not found.")

            # ── Step 5: extract price and stock ──────────────────────────────
            await emit("Reading price and stock from csavarda.hu…")
            price_str = await page.evaluate(_JS_NEXT_SIBLING, "Nettó egységár:")
            buda_str  = await page.evaluate(_JS_CONTAINS, "Budapest:")
            vecs_str  = await page.evaluate(_JS_CONTAINS, "Vecsés:")

            log.info(f"Raw extracted values — price: '{price_str}', budapest: '{buda_str}', vecsés: '{vecs_str}'")

            if not price_str:
                raise RuntimeError("Could not read price from csavarda.hu. Page layout may have changed.")

            from agent.tools import parse_price_string, parse_stock_string
            price_raw, price_unit_qty, unit = parse_price_string(price_str)
            log.info(f"Parsed price: {price_raw} HUF / {price_unit_qty} {unit}")

            result = {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "HUF",
                "unit":             unit,
                "stock": {
                    "budapest": parse_stock_string(buda_str) if buda_str else 0,
                    "vecsés":   parse_stock_string(vecs_str) if vecs_str else 0,
                },
                "queried_at": datetime.now().isoformat(timespec="seconds"),
            }
            log.info(f"Final result: {result}")
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during csavarda.hu scrape: {exc}")
            raise RuntimeError(f"csavarda.hu scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
