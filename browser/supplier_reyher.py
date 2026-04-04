"""
Playwright scraper for rio.reyher.de (Supplier F)

Login flow:
  1. Load session from assets/sessions/reyher_session.json
     - Skip restore if saved_at > 23 h old (proactive re-login before expiry)
  2. Verify session with a positive authenticated marker:
     look for the 'Quickinput' nav link that only appears for logged-in users
  3. If session missing / stale / invalid: full login, save new session with timestamp
     - important: submit the login form with Enter from the password field;
       the visible button click is not reliable in headless mode

Search + price flow:
  4. Navigate directly to the advanced-search URL (?sku={part_no}&q=) with networkidle
     — networkidle is required: price data is injected by a SAP AJAX call after load
  5. Validate result count via DOM (structured), text check as fallback
  6. Open the product modal from the result row
  7. Read the real own price from the modal:
     "Own price/{N} Pcs" + "{price} €"
  8. Quantity comes from the own-price label first, Quantity/PU second, table/header only as fallback
  9. Stock from modal "Available quantity"

Price normalisation:
  price_raw=7.50, price_unit_qty=100 → tools.py yields price_per_db=0.075 EUR/db

Currency: EUR (German supplier)
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

log = logging.getLogger("reyher")

LOGIN_URL  = "https://rio.reyher.de/hu/customer/account/login"
HOME_URL   = "https://rio.reyher.de/hu/"
SEARCH_URL = "https://rio.reyher.de/hu/catalogsearch/advanced/result/?sku={part_no}&q="
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "reyher_session.json"

_SESSION_MAX_AGE_HOURS = 23
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

    # Live testing showed that plain fill() is flaky on this login page:
    # 3 fresh attempts with fill() only logged in once, while real typing with
    # a short blur/Tab after each field logged in 3/3 times.
    for selector, value in (
        ("#customernumber", customer_code),
        ("#userid", username),
        ("#pass", password),
    ):
        field = page.locator(selector)
        await field.click()
        await field.press("Control+A")
        await field.press("Backspace")
        await field.type(value, delay=35)
        await page.wait_for_timeout(150)
        await field.press("Tab")
        await page.wait_for_timeout(200)

    filled = await page.evaluate(
        """() => ({
            customer_ok: !!document.querySelector('#customernumber')?.value,
            user_ok: !!document.querySelector('#userid')?.value,
            pass_len: (document.querySelector('#pass')?.value || '').length,
        })"""
    )
    log.info(
        "Login form filled: customer=%r user=%r pass_len=%s",
        filled["customer_ok"],
        filled["user_ok"],
        filled["pass_len"],
    )

    # In live testing the visible button click kept the page on the login form,
    # while pressing Enter from the password field created a valid authenticated
    # session and exposed the Quickinput navigation.
    await page.locator("#pass").press("Enter")
    log.info("Submitted Reyher login via Enter on password field")

    try:
        await page.wait_for_function(
            """() => {
                const txt = document.body ? (document.body.innerText || '') : '';
                const hasQuickinput =
                    !!document.querySelector("a[href*='quickinput']") ||
                    txt.includes('Quickinput');
                const stillOnLogin =
                    location.href.includes('/customer/account/login') ||
                    !!document.querySelector('#customernumber') ||
                    !!document.querySelector('#userid') ||
                    !!document.querySelector('#pass');
                return hasQuickinput || !stillOnLogin;
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout:
        log.warning("Reyher login did not expose Quickinput within timeout")

    if not await _is_logged_in(page):
        log.warning("Reyher login state after submit: url=%s", page.url)
        try:
            body_text = await page.locator("body").inner_text()
            log.warning("Reyher login failure body snippet: %s", body_text[:1200])
        except Exception:
            pass
        raise RuntimeError("Login to rio.reyher.de failed — check credentials.")

    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeout:
        log.info("networkidle timeout after successful Reyher login — continuing")
    log.info("Login successful (Quickinput visible)")


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


def _parse_unit_qty_from_header(header_text: str) -> int:
    """Fallback only: extract unit quantity from a header like 'Katalógusár/100' → 100."""
    m = re.search(r"Katalóguság[^0-9]*(\d+)|Katalógusár[^0-9]*(\d+)", header_text, re.IGNORECASE)
    if m:
        return int(m.group(1) or m.group(2))
    return 100   # stated in the page footer: "per 100 pieces"


async def _extract_price_cell_text(page) -> str | None:
    """Structured first-row read with a JS fallback for layout quirks."""
    locator = page.locator("table.table tbody tr:first-child .productlist_table-column-value").first
    try:
        text = await locator.inner_text(timeout=5000)
        text = text.strip()
        if text:
            return text
    except PlaywrightTimeout:
        pass

    return await page.evaluate("""() => {
        const col = document.querySelector(
            'table.table tbody tr:first-child .productlist_table-column-value'
        );
        return col ? col.innerText.trim() : null;
    }""")


async def _extract_header_text(page) -> str:
    try:
        headers = await page.locator("table.table thead th").all_inner_texts()
        text = " ".join(h.strip() for h in headers if h and h.strip())
        if text:
            return text
    except Exception:
        pass
    return await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('table.table thead th'))
            .map(th => th.innerText.trim()).join(' ');
    }""")


async def _open_product_modal(page) -> None:
    link = page.locator("a.productlist_item_anchor.js-Modal").first
    if await link.count() == 0:
        raise RuntimeError("Reyher product modal trigger was not found in the first result row.")
    await link.click(timeout=8000)
    await page.wait_for_function(
        """() => {
            const txt = document.body ? (document.body.innerText || '') : '';
            return txt.includes('Own price');
        }""",
        timeout=15000,
    )


async def _extract_modal_price_info(page) -> dict | None:
    return await page.evaluate("""() => {
        const blocks = [...document.querySelectorAll('.modal--open .price, .modal--open .productdetails__overview, .modal--open .tab-pane, .modal--open .modal__content')];
        const texts = blocks.map(el => (el.innerText || '').trim()).filter(Boolean);
        const joined = texts.join('\\n');
        if (!joined.includes('Own price')) return null;

        const priceMatch = joined.match(/Own price\\s*\\/\\s*(\\d+)\\s*Pcs[\\s\\S]*?(\\d+[\\.,]\\d+)\\s*€/i);
        const qtyPuMatch = joined.match(/Quantity\\/PU:\\s*(\\d+)\\s*pcs/i);
        const stockMatch = joined.match(/Available quantity:\\s*(\\d+)/i);

        return {
            text: joined.slice(0, 2500),
            own_qty: priceMatch ? parseInt(priceMatch[1], 10) : null,
            own_price: priceMatch ? priceMatch[2] : null,
            quantity_pu: qtyPuMatch ? parseInt(qtyPuMatch[1], 10) : null,
            stock: stockMatch ? parseInt(stockMatch[1], 10) : null,
        };
    }""")


def _parse_first_int(text: str) -> int | None:
    m = re.search(r"\b(\d{1,6})\b", text or "")
    return int(m.group(1)) if m else None


async def _extract_row_unit_qty(page, price_cell_text: str) -> int | None:
    """Prefer row-specific qty over header defaults.

    Order:
      1. First line of the price cell ("100\\n110,00 EUR" -> 100)
      2. Dedicated db column in the same row
      3. Numeric cell immediately before the price cell
    """
    first_line = (price_cell_text or "").splitlines()[0].strip() if price_cell_text else ""
    qty = _parse_first_int(first_line)
    if qty:
        return qty

    try:
        row_data = await page.evaluate("""() => {
            const row = document.querySelector('table.table tbody tr:first-child');
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td')).map((td, idx) => ({
                idx,
                text: (td.innerText || '').trim(),
                cls: td.className || '',
            }));
            const priceIdx = cells.findIndex(c => c.text.includes('EUR'));
            return {cells, priceIdx};
        }""")
    except Exception:
        row_data = None

    if not row_data:
        return None

    cells = row_data.get("cells") or []
    price_idx = row_data.get("priceIdx", -1)

    for idx, cell in enumerate(cells):
        txt = cell.get("text", "")
        if idx == price_idx:
            continue
        if re.fullmatch(r"\d{1,6}", txt):
            return int(txt)

    if isinstance(price_idx, int) and price_idx > 0:
        prev_text = cells[price_idx - 1].get("text", "")
        qty = _parse_first_int(prev_text)
        if qty:
            return qty

    return None


async def _log_search_state(page, supplier_part_no: str, prefix: str = "Reyher") -> None:
    try:
        data = await page.evaluate(
            """(partNo) => {
                const txt = document.body ? (document.body.innerText || '') : '';
                return {
                    url: location.href,
                    quickinput: !!document.querySelector("a[href*='quickinput']") || txt.includes('Quickinput'),
                    table_rows: document.querySelectorAll('table.table tbody tr').length,
                    price_cols: document.querySelectorAll('.productlist_table-column-value').length,
                    eur_hits: txt.split('EUR').length - 1,
                    snippet: txt.slice(Math.max(0, txt.toLowerCase().indexOf(partNo.toLowerCase()) - 300), Math.max(0, txt.toLowerCase().indexOf(partNo.toLowerCase()) - 300) + 2200),
                };
            }""",
            supplier_part_no,
        )
        log.info(
            "%s state: url=%s quickinput=%r rows=%s price_cols=%s eur_hits=%s",
            prefix,
            data["url"],
            data["quickinput"],
            data["table_rows"],
            data["price_cols"],
            data["eur_hits"],
        )
        log.info("%s snippet: %s", prefix, data["snippet"])
    except Exception as exc:
        log.warning("%s state logging failed: %s", prefix, exc)


# ── Main scraper ───────────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, _SESSION_MAX_AGE_HOURS))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        if use_session:
            log.info("Restoring saved session (age < 23 h)")
            try:
                context = await browser.new_context(storage_state=session["state"])
            except Exception as exc:
                log.warning(f"Could not restore session state: {exc}")
                use_session = False
                invalidate_session(SESSION_FILE)
                context = await browser.new_context()
        else:
            if session:
                log.info("Session stale (> 23 h) — proactive re-login")
                invalidate_session(SESSION_FILE)
            context = await browser.new_context()

        page = await context.new_page()

        try:
            await emit("Opening rio.reyher.de…")

            # ── Step 1: restore session or do fresh login ──────────────────────
            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)

                if not await _is_logged_in(page):
                    log.warning("Restored session is no longer valid — re-logging in")
                    use_session = False
                    invalidate_session(SESSION_FILE)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                else:
                    log.info("Session valid (Quickinput nav link present)")

            if not use_session:
                await _login(page, emit)
                await save_session(context, SESSION_FILE)

            # ── Step 2: navigate to search results ────────────────────────────
            await emit(f"Searching for {supplier_part_no} on rio.reyher.de…")
            search_url = SEARCH_URL.format(part_no=supplier_part_no)

            # networkidle is required: the SAP AJAX call that injects the price
            # fires after domcontentloaded, and networkidle ensures it has completed.
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            log.info(f"Search page loaded: {page.url}")
            await _log_search_state(page, supplier_part_no, prefix="Reyher search")

            # ── Step 3: validate result count (structured first) ───────────────
            result_rows = await page.locator("table.table tbody tr").count()
            if result_rows == 0:
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            # Text fallback for "no results" pages that still render the table skeleton
            body_text = await page.locator("body").inner_text()
            if "Nem található" in body_text or "0 árucikket eredményezett" in body_text:
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            log.info(f"{result_rows} result row(s) found for {supplier_part_no}")

            # ── Step 4: open product modal and extract own price ───────────────
            await emit("Reading price and stock from rio.reyher.de…")

            modal_info = None
            try:
                await _open_product_modal(page)
                modal_info = await _extract_modal_price_info(page)
                log.info("Reyher modal info: %r", modal_info)
            except PlaywrightTimeout:
                log.warning("Reyher product modal did not expose Own price within timeout")

            if modal_info and modal_info.get("own_price"):
                price_raw = _parse_price(modal_info["own_price"])
                price_unit_qty = modal_info.get("own_qty") or modal_info.get("quantity_pu") or 100
                stock_value = modal_info.get("stock")
                log.info(
                    "Reyher modal own price parsed: %s EUR / %s pcs, stock=%s",
                    price_raw,
                    price_unit_qty,
                    stock_value,
                )
            else:
                log.info("Reyher modal own price missing — falling back to search table price")

                _PRICE_JS = """() => {
                    const col = document.querySelector(
                        'table.table tbody tr:first-child .productlist_table-column-value'
                    );
                    return col && col.innerText.includes('EUR');
                }"""

                try:
                    await page.wait_for_function(_PRICE_JS, timeout=15000)
                    log.info("Price appeared in search table without extra click")
                except PlaywrightTimeout:
                    log.warning("Reyher search table price still absent")
                    await _log_search_state(page, supplier_part_no, prefix="Reyher price-missing")

                price_cell_text = await _extract_price_cell_text(page)

                log.info(f"Price cell raw text: {price_cell_text!r}")

                if not price_cell_text or "EUR" not in price_cell_text:
                    raise RuntimeError(
                        f"Price not found for {supplier_part_no} on rio.reyher.de. "
                        "Neither the product modal Own price nor the search table price was available."
                    )

                price_match = re.search(r"([\d,.]+)\s*\xa0?EUR", price_cell_text)
                if not price_match:
                    raise RuntimeError(f"Could not parse EUR price from: {price_cell_text!r}")
                price_raw = _parse_price(price_match.group(1))

                price_unit_qty = await _extract_row_unit_qty(page, price_cell_text)
                if price_unit_qty:
                    log.info("Reyher row-specific unit_qty=%s from result row", price_unit_qty)
                else:
                    header_text = await _extract_header_text(page)
                    price_unit_qty = _parse_unit_qty_from_header(header_text)
                    log.info(
                        "Reyher row-specific qty missing, header fallback %r → unit_qty=%s",
                        header_text,
                        price_unit_qty,
                    )

                stock_value = None
                log.info("Stock not available in search-table fallback — returning None")

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
