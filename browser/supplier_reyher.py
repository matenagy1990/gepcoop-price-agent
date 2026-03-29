"""
Playwright scraper for rio.reyher.de (Supplier F)

Login flow:
  1. Try to restore session from assets/sessions/reyher_session.json
  2. If session invalid/missing: login with credentials, save new session

Search + price flow:
  3. Fill Cikkszám search field → press Enter → wait_for_selector("table.table tbody tr")
  4. Click td:nth-child(2) of the first result row (part number cell) → opens detail panel
  5. wait_for_function: body.innerText.includes('Own price')
     — covers both panel-open AND SAP AJAX response arriving (single condition)
  6. Text-walker extraction — anchors on label text only, no class/ID/data-bind deps:
       find text node starting with "own price" → grandparent row → children[1] = value
       unit qty parsed from label text "/100 Pcs" → 100

Data extraction (rendered detail panel — layout-agnostic):
  - Price:    text-walker finds "Own price…" → next numeric sibling → e.g. "27,47"
  - Unit qty: parsed from label text "/100 Pcs" → 100
  - Stock:    text-walker finds "Available quantity:" → next sibling value

Price normalisation:
  price_raw=27.47, price_unit_qty=100 → tools.py yields price_per_db=0.2747 EUR/db

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

LOGIN_URL    = "https://rio.reyher.de/hu/customer/account/login"
HOME_URL     = "https://rio.reyher.de/hu/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "reyher_session.json"


def _load_saved_cookies() -> list | None:
    """Return saved cookies or None if not present / unreadable."""
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


def _save_cookies(cookies: list) -> None:
    """Persist cookies to disk."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(cookies, indent=2))
        log.info(f"Session saved to {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _is_logged_in(page) -> bool:
    """Return True if the current page shows an authenticated session."""
    body = await page.locator("body").inner_text()
    return "Bejelentkezve" in body or "Fiókom" in body


async def _login(page, emit) -> None:
    """Perform a full login and wait for the home page."""
    customer_code = os.getenv("SUPPLIER_F_CUSTOMER_CODE", "")
    username      = os.getenv("SUPPLIER_F_USERNAME", "")
    password      = os.getenv("SUPPLIER_F_PASSWORD", "")
    log.info(f"Logging in as customer {customer_code} / user {username}")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.info(f"Login page loaded: {page.url}")

    # Accept cookie banner
    try:
        await page.get_by_role("button", name="Allow all").click(timeout=5000)
        await page.wait_for_timeout(800)
        log.info("Cookie banner accepted")
    except PlaywrightTimeout:
        pass

    await emit("Logging in to rio.reyher.de…")
    await page.get_by_role("textbox", name="Ügyfélszám").fill(customer_code)
    await page.get_by_role("textbox", name="Felhasználónév").fill(username)
    await page.get_by_role("textbox", name="Jelszó").fill(password)
    await page.get_by_role("button", name="Bejelentkezés").click()

    try:
        await page.wait_for_url(HOME_URL, timeout=15000)
        log.info(f"Login successful: {page.url}")
    except PlaywrightTimeout:
        raise RuntimeError("Login to rio.reyher.de failed. Please check credentials.")


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

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await context.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                # Verify the session is still valid; re-login if expired
                if not await _is_logged_in(page):
                    log.warning("Saved session is expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await _login(page, emit)
                    cookies = await context.cookies()
                    _save_cookies(cookies)
                else:
                    log.info("Session restored successfully")
            else:
                await _login(page, emit)
                cookies = await context.cookies()
                _save_cookies(cookies)

            # --- 3. Search ---
            await emit(f"Searching for {supplier_part_no} on rio.reyher.de…")
            await page.get_by_role("textbox", name="Cikkszám").fill(supplier_part_no)
            await page.get_by_role("textbox", name="Cikkszám").press("Enter")

            # Wait only for the results table — much faster than networkidle
            # (networkidle blocks on analytics/SAP polling requests for 20-25s)
            await page.wait_for_selector("table.table tbody tr", timeout=15000)
            log.info(f"Search results table loaded: {page.url}")

            # --- 4. Check results ---
            body_text = await page.locator("body").inner_text()
            if "Nem található" in body_text or "0 találat" in body_text.lower():
                raise RuntimeError(f"Part {supplier_part_no} was not found on rio.reyher.de.")

            # Click the part number div (the cursor:pointer element inside td:nth-child(2)).
            # Clicking the <td> itself is unreliable — the panel is opened by the inner <div>.
            # get_by_text scoped to the table finds exactly that div.
            await page.locator("table.table").get_by_text(supplier_part_no, exact=True).click(timeout=8000)
            log.info(f"Clicked part number div for {supplier_part_no} — detail panel opening")

            # Wait for the SAP AJAX response to populate the "Own price" field.
            # "Own price" only appears in body.innerText after the panel is open
            # AND the SAP call has completed — this single wait covers both conditions.
            await page.wait_for_function(
                "() => document.body.innerText.includes('Own price')",
                timeout=20000,
            )
            log.info("Own price visible in DOM — detail panel fully loaded")

            await emit("Reading price and stock from rio.reyher.de…")

            # --- Price extraction ---
            # Text-walker approach: anchors on the "Own price" label text only.
            # Robust against CSS class / layout changes.
            #
            # Rendered DOM structure in the detail panel:
            #   <div>                            ← grandparent row
            #     <div>Own price/100 Pcs</div>   ← label  (children[0])
            #     <div>27,47</div>               ← value  (children[1])
            #     <div>€</div>                   ← unit   (children[2])
            #   </div>
            own_price_data = await page.evaluate("""() => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.textContent.trim();
                    if (!text.toLowerCase().startsWith('own price')) continue;

                    const labelEl = node.parentElement;       // div with label text
                    const row     = labelEl.parentElement;    // parent div (the row)
                    const siblings = Array.from(row.children);
                    const labelIdx = siblings.indexOf(labelEl);

                    // Unit qty from label: "Own price/100 Pcs" → 100
                    const qtyMatch = text.match(/\\/(\\d[\\d,]*)/);
                    const qty = qtyMatch ? parseInt(qtyMatch[1].replace(',', '')) : 100;

                    // Price: first sibling after the label whose text looks like a number
                    for (let i = labelIdx + 1; i < siblings.length; i++) {
                        const val = siblings[i].textContent.trim();
                        if (/^[\\d][\\d.,]*$/.test(val)) {
                            return { price: val, qty };
                        }
                    }
                }
                return null;
            }""")

            log.info(f"Own price data: {own_price_data}")

            if not own_price_data or not own_price_data.get("price"):
                log.error(f"Own price not found. Body sample: {body_text[:500]}")
                raise RuntimeError(
                    "Could not read own price from rio.reyher.de. "
                    "Check that the account has customer-specific pricing enabled."
                )

            price_text     = own_price_data["price"]
            price_unit_qty = own_price_data["qty"]

            if "," in price_text and "." in price_text:
                price_text = price_text.replace(".", "").replace(",", ".")
            elif "," in price_text:
                price_text = price_text.replace(",", ".")
            price_raw = float(price_text)

            # --- Stock extraction ---
            # Same text-walker pattern for "Available quantity:" label.
            stock_data = await page.evaluate("""() => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.textContent.trim();
                    if (!text.toLowerCase().includes('available quantity')) continue;

                    const labelEl  = node.parentElement;
                    const row      = labelEl.parentElement;
                    const siblings = Array.from(row.children);
                    const idx      = siblings.indexOf(labelEl);
                    const valueEl  = siblings[idx + 1];
                    return valueEl ? valueEl.textContent.trim() : null;
                }
                return null;
            }""")
            log.info(f"Stock data: {stock_data!r}")
            stock_value = int(re.sub(r"[^\d]", "", stock_data)) if stock_data else None

            log.info(f"Parsed — price_raw: {price_raw} EUR / {price_unit_qty} db, stock: {stock_value}")

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
