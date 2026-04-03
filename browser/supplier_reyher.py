"""
Playwright scraper for rio.reyher.de (Supplier F)

Login flow:
  1. Load session from assets/sessions/reyher_session.json
     - Skip restore if saved_at > 23 h old (proactive re-login before expiry)
  2. Verify session with a positive authenticated marker:
     look for the 'Quickinput' nav link that only appears for logged-in users
  3. If session missing / stale / invalid: full login, save new session with timestamp

Search + price flow:
  4. Navigate directly to the advanced-search URL (?sku={part_no}&q=) with networkidle
     — networkidle is required: price data is injected by a SAP AJAX call after load
  5. Validate result count via DOM (structured), text check as fallback
  6. Read price from div.productlist_table-column-value — already populated by networkidle
     Format: "{pack_qty}\\n{price} EUR"  e.g. "200\\n7,50\\xa0EUR"
  7. Parse unit qty from thead — "Katalógusár/{N}" header cell
  8. Stock: not exposed on search results page → None

Price normalisation:
  price_raw=7.50, price_unit_qty=100 → tools.py yields price_per_db=0.075 EUR/db

Currency: EUR (German supplier)
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

log = logging.getLogger("reyher")

LOGIN_URL  = "https://rio.reyher.de/hu/customer/account/login"
HOME_URL   = "https://rio.reyher.de/hu/"
SEARCH_URL = "https://rio.reyher.de/hu/catalogsearch/advanced/result/?sku={part_no}&q="
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "reyher_session.json"

_SESSION_MAX_AGE_HOURS = 23


# ── Session helpers ────────────────────────────────────────────────────────────

def _load_session() -> dict | None:
    """Return session dict {saved_at, cookies} or None if missing / unreadable."""
    try:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text())
            if isinstance(data, list):           # legacy format (just cookies list)
                return {"saved_at": None, "cookies": data}
            return data
    except Exception:
        pass
    return None


def _save_session(cookies: list) -> None:
    """Persist session cookies with an ISO timestamp for age-checking."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(
            {"saved_at": datetime.now().isoformat(timespec="seconds"), "cookies": cookies},
            indent=2,
        ))
        log.info(f"Session saved → {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


def _session_is_fresh(session: dict) -> bool:
    """Return True if session was saved less than _SESSION_MAX_AGE_HOURS ago."""
    saved_at = session.get("saved_at")
    if not saved_at:
        return False
    try:
        age_h = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 3600
        return age_h < _SESSION_MAX_AGE_HOURS
    except Exception:
        return False


# ── Auth helpers ───────────────────────────────────────────────────────────────

async def _is_logged_in(page) -> bool:
    """Positive auth check: look for the Quickinput nav link only shown to logged-in users."""
    if "/customer/account/login" in page.url:
        return False
    try:
        await page.wait_for_selector("a:has-text('Quickinput')", timeout=4000)
        return True
    except PlaywrightTimeout:
        return False


async def _login(page, emit) -> None:
    """Full login flow. Raises RuntimeError on failure."""
    customer_code = os.getenv("SUPPLIER_F_CUSTOMER_CODE", "")
    username      = os.getenv("SUPPLIER_F_USERNAME", "")
    password      = os.getenv("SUPPLIER_F_PASSWORD", "")

    # Mask sensitive data in logs
    log.info(
        f"Logging in — customer ...{customer_code[-3:]}, user ...{username[-3:] if len(username) > 3 else '***'}"
    )

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.info(f"Login page loaded: {page.url}")

    # Already authenticated (e.g. cookie restored, redirect happened)
    if "/customer/account/login" not in page.url:
        log.info("Redirected away from login page — already authenticated, skipping fill")
        return

    # Dismiss cookie consent banner (try known button labels)
    for btn_name in ("Allow all", "Mindent engedélyez", "Accept all"):
        try:
            await page.get_by_role("button", name=btn_name).click(timeout=3000)
            await page.wait_for_timeout(400)
            log.info(f"Cookie banner dismissed ({btn_name!r})")
            break
        except PlaywrightTimeout:
            continue

    await emit("Logging in to rio.reyher.de…")

    # ID-based fill — more reliable than get_by_role+name when the cookie banner
    # overlay interferes with the accessibility tree in headless mode.
    await page.locator("#customernumber").fill(customer_code)
    await page.locator("#userid").fill(username)
    await page.locator("#pass").fill(password)
    await page.get_by_role("button", name="Bejelentkezés").click()

    try:
        await page.wait_for_url(HOME_URL, timeout=15000)
        # Wait for networkidle on the home page so the SAP session is fully initialised
        # before navigating to the search/results page.
        await page.wait_for_load_state("networkidle", timeout=20000)
        log.info("Login successful (home page fully loaded)")
    except PlaywrightTimeout:
        if "/customer/account/login" in page.url:
            raise RuntimeError("Login to rio.reyher.de failed — check credentials.")
        log.warning("networkidle timeout on home page after login — continuing anyway")


# ── Price helpers ──────────────────────────────────────────────────────────────

def _parse_price(price_text: str) -> float:
    """Parse a German/English formatted price string to float.
    Examples: '7,50' → 7.50 | '1.234,56' → 1234.56 | '7.50' → 7.50
    """
    t = price_text.strip()
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")   # '1.234,56' → '1234.56'
    else:
        t = t.replace(",", ".")
    return float(t)


def _parse_unit_qty(header_text: str) -> int:
    """Extract unit quantity from a header like 'Katalógusár/100' → 100."""
    m = re.search(r"Katalóguság[^0-9]*(\d+)|Katalógusár[^0-9]*(\d+)", header_text, re.IGNORECASE)
    if m:
        return int(m.group(1) or m.group(2))
    return 100   # stated in the page footer: "per 100 pieces"


# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        try:
            await emit("Opening rio.reyher.de…")

            # ── Step 1: restore session or do fresh login ──────────────────────
            session = _load_session()

            if session and _session_is_fresh(session):
                log.info("Restoring session cookies (age < 23 h)")
                await context.add_cookies(session["cookies"])
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)

                if not await _is_logged_in(page):
                    log.warning("Restored session is no longer valid — re-logging in")
                    SESSION_FILE.unlink(missing_ok=True)
                    await _login(page, emit)
                    _save_session(await context.cookies())
                else:
                    log.info("Session valid (Quickinput nav link present)")
            else:
                if session:
                    log.info("Session is stale (> 23 h) — proactive re-login")
                    SESSION_FILE.unlink(missing_ok=True)
                else:
                    log.info("No saved session — performing fresh login")
                await _login(page, emit)
                _save_session(await context.cookies())

            # ── Step 2: navigate to search results ────────────────────────────
            await emit(f"Searching for {supplier_part_no} on rio.reyher.de…")
            search_url = SEARCH_URL.format(part_no=supplier_part_no)

            # networkidle is required: the SAP AJAX call that injects the price
            # fires after domcontentloaded, and networkidle ensures it has completed.
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            log.info(f"Search page loaded: {page.url}")

            # ── Step 3: validate result count (structured first) ───────────────
            result_rows = await page.locator("table.table tbody tr").count()
            if result_rows == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            # Text fallback for "no results" pages that still render the table skeleton
            body_text = await page.locator("body").inner_text()
            if "Nem található" in body_text or "0 árucikket eredményezett" in body_text:
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            log.info(f"{result_rows} result row(s) found for {supplier_part_no}")

            # ── Step 4: extract price from the first result row ────────────────
            await emit("Reading price and stock from rio.reyher.de…")

            # The SAP AJAX call that injects the price fires asynchronously.
            # networkidle usually captures it, but not always (timing race).
            # Strategy:
            #   1. Wait up to 15 s for EUR to appear without any interaction
            #   2. If still missing, click the first result row to trigger the SAP call
            #   3. Wait another 10 s
            _PRICE_JS = """() => {
                const col = document.querySelector(
                    'table.table tbody tr:first-child .productlist_table-column-value'
                );
                return col && col.innerText.includes('EUR');
            }"""

            try:
                await page.wait_for_function(_PRICE_JS, timeout=15000)
                log.info("Price appeared without click (SAP call completed)")
            except PlaywrightTimeout:
                log.info("Price not yet loaded — clicking first result row to trigger SAP call")
                await page.locator("table.table tbody tr:first-child td:nth-child(2)").click(timeout=8000)
                try:
                    await page.wait_for_function(_PRICE_JS, timeout=15000)
                    log.info("Price appeared after row click")
                except PlaywrightTimeout:
                    log.warning("Price still absent after click — proceeding anyway")

            price_cell_text = await page.evaluate("""() => {
                const col = document.querySelector(
                    'table.table tbody tr:first-child .productlist_table-column-value'
                );
                return col ? col.innerText.trim() : null;
            }""")

            log.info(f"Price cell raw text: {price_cell_text!r}")

            if not price_cell_text or "EUR" not in price_cell_text:
                raise RuntimeError(
                    f"Price not found in search results for {supplier_part_no} on rio.reyher.de. "
                    "The account may lack pricing access, or the page layout changed."
                )

            # Extract EUR amount: "200\n7,50\xa0EUR" → "7,50"
            price_match = re.search(r"([\d,.]+)\s*\xa0?EUR", price_cell_text)
            if not price_match:
                raise RuntimeError(f"Could not parse EUR price from: {price_cell_text!r}")
            price_raw = _parse_price(price_match.group(1))

            # ── Step 5: determine unit quantity from table header ──────────────
            header_text = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('table.table thead th'))
                    .map(th => th.innerText.trim()).join(' ');
            }""")
            price_unit_qty = _parse_unit_qty(header_text)
            log.info(f"Header text: {header_text!r} → unit_qty={price_unit_qty}")

            # ── Step 6: stock ─────────────────────────────────────────────────
            # Stock is not exposed on the search results page via networkidle.
            # The stock placeholder (data-id="sp-...") requires a separate authenticated
            # AJAX call not triggered by the page load.
            stock_value = None
            log.info("Stock: not available on search results page — returning None")

            log.info(
                f"Parsed — price_raw={price_raw} EUR / {price_unit_qty} db, stock={stock_value}"
            )

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
            log.exception(f"Unexpected error during rio.reyher.de scrape: {exc}")
            raise RuntimeError(f"rio.reyher.de scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
