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

HOME_URL = "https://eshop.mekrs.cz/en"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "mekrs_session.json"
_SESSION_MAX_AGE_H = 20


# ── Session helpers ────────────────────────────────────────────────────────────

def _load_session() -> dict | None:
    try:
        if _SESSION_FILE.exists():
            data = json.loads(_SESSION_FILE.read_text())
            if isinstance(data, dict) and "state" in data:
                return data
    except Exception:
        pass
    return None


def _session_is_fresh(session: dict) -> bool:
    saved_at = session.get("saved_at")
    if not saved_at:
        return False
    try:
        age_h = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 3600
        return age_h < _SESSION_MAX_AGE_H
    except Exception:
        return False


async def _save_session(context) -> None:
    try:
        state = await context.storage_state()
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(
            {"saved_at": datetime.now().isoformat(timespec="seconds"), "state": state},
            indent=2,
        ))
        log.info(f"Session saved → {_SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


def _parse_stock(s: str) -> int:
    """Extract stock number from 'In stock 1,653,361 pcs' → 1653361"""
    if not s:
        return 0
    digit_groups = re.findall(r"\d+", s)
    return int("".join(digit_groups)) if digit_groups else 0


# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session     = _load_session()
    use_session = session and _session_is_fresh(session)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        if use_session:
            log.info("Restoring saved session (age < 20 h)")
            try:
                context = await browser.new_context(storage_state=session["state"])
            except Exception as exc:
                log.warning(f"Could not restore session state: {exc}")
                use_session = False
                _SESSION_FILE.unlink(missing_ok=True)
                context = await browser.new_context()
        else:
            if session:
                log.info("Session stale (> 20 h) — proactive re-login")
                _SESSION_FILE.unlink(missing_ok=True)
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
                try:
                    await page.wait_for_selector("input[name='username'], input[placeholder='Search by name, code, DIN']", timeout=8000)
                except PlaywrightTimeout:
                    log.warning("No login/search marker appeared after session restore — continuing check")
                log.info(f"After session restore, URL: {page.url}")

                login_form_visible = await page.locator("input[name='username']").first.is_visible()
                if login_form_visible:
                    log.warning("Session invalid (login form visible) — falling back to full login")
                    use_session = False
                    _SESSION_FILE.unlink(missing_ok=True)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                    page.on("dialog", handle_dialog)
                else:
                    log.info("Session valid — login skipped")

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
                try:
                    await page.wait_for_selector("input[placeholder='Search by name, code, DIN']", timeout=12000)
                except PlaywrightTimeout:
                    pass

                if await page.locator("input[name='username']").first.is_visible():
                    raise RuntimeError("Login to eshop.mekrs.cz failed. Please check credentials.")

                log.info(f"Login successful — URL: {page.url}")
                await _save_session(context)

            # ── Step 3: search via autocomplete ──────────────────────────────
            await emit(f"Searching for part {supplier_part_no} on eshop.mekrs.cz…")
            search_inp = page.locator("input[placeholder='Search by name, code, DIN']").first
            await search_inp.click()
            await search_inp.type(supplier_part_no, delay=50)
            log.info(f"Typed '{supplier_part_no}' into search box, waiting for autocomplete…")
            try:
                await page.wait_for_selector("text=Show all results", timeout=8000)
            except PlaywrightTimeout:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(autocomplete returned no results)."
                )

            show_all = page.locator("text=Show all results").first
            show_all_count = await show_all.count()
            if show_all_count == 0:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(autocomplete returned no results)."
                )
            await show_all.click()
            try:
                await page.wait_for_selector("[data-testid='product-card']", timeout=10000)
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

            card_count = await page.locator("[data-testid='product-card']").count()
            log.info(f"Product cards on results page: {card_count}")
            if card_count == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on eshop.mekrs.cz.")

            # ── Step 4: extract data ──────────────────────────────────────────
            await emit("Reading price and stock from eshop.mekrs.cz…")
            first_card = page.locator("[data-testid='product-card']").first

            try:
                stock_str = await first_card.locator(
                    "div.text-sm.font-medium.text-primaryGreen"
                ).inner_text(timeout=5000)
                stock_str = stock_str.strip()
            except PlaywrightTimeout:
                stock_str = ""
                log.warning("Stock element not found — assuming out of stock")

            price_str, unit_str, price_elem_html = await page.evaluate("""() => {
                const leaves = Array.from(document.querySelectorAll('*'))
                    .filter(el => el.childElementCount === 0);

                for (const unitEl of leaves) {
                    const ut = unitEl.textContent.trim();
                    if (!/\\/\\s*\\d[\\d,]*\\s*pcs/.test(ut)) continue;

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
            log.info(f"Raw — price: '{price_str}', unit: '{unit_str}', stock: '{stock_str}'")

            if not price_str:
                body_snippet = await page.evaluate(
                    "() => document.body.innerHTML.slice(0, 3000)"
                )
                log.error(f"Price not found — body HTML snippet:\n{body_snippet}")
                raise RuntimeError(
                    "Could not read price from eshop.mekrs.cz. Page layout may have changed."
                )

            price_clean = re.sub(r"[^\d.,]", "", price_str)
            if "," in price_clean:
                price_clean = price_clean.replace(",", "")
            price_raw = float(price_clean)

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
