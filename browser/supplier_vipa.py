"""Playwright scraper for www.vipafasteners.com (Vipa).

Login is OTP-based — a one-time token is sent to the registered email address.
When no valid session exists, raises RuntimeError(VIPA_OTP_REQUIRED) so
main.py can emit a password_required event with otp=True.

A fresh session is established via:
  POST /vipa/initiate-login  — sends OTP email, keeps browser open, waits
  POST /vipa/complete-login  — submits the token, saves the session file
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh

load_dotenv()

log = logging.getLogger("vipa")

HOME_URL    = "https://www.vipafasteners.com/en/"
LOGIN_URL   = "https://www.vipafasteners.com/en/login/"
SEARCH_URL  = "https://www.vipafasteners.com/en/search/?q={part_no}"
PRICE_PREVIEW_URL = "https://www.vipafasteners.com/en/j/price-preview/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "vipa_session.json"
SESSION_MAX_AGE_HOURS = 20

# Raised when no valid session exists — caught in main.py to emit OTP event
VIPA_OTP_REQUIRED = (
    "Vipa: aktív session nem áll rendelkezésre – "
    "OTP bejelentkezés szükséges (token az e-mailben)"
)


async def _is_logged_in(page) -> bool:
    try:
        return await page.locator('a[href*="/en/customer/"]').count() > 0
    except Exception:
        return False


def _parse_stock(text: str) -> int:
    """Extract integer quantity from 'In stock: 1234 Pcs' style text."""
    m = re.search(r"In stock:\s*([\d\s\.,]+)", text or "", re.IGNORECASE)
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 0


def _parse_price_per_qty(text: str) -> tuple[float, int] | None:
    """
    Parse '546,91 €/1000' or '54,69 €/100' into (price_raw, price_unit_qty).
    Returns None if no match.
    """
    m = re.search(r"([0-9 .,\u00a0]+)\s*€\s*/\s*([0-9 .,\u00a0]+)", text or "")
    if not m:
        return None
    price_str = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    qty_str = re.sub(r"\D", "", m.group(2))
    try:
        return float(price_str), int(qty_str)
    except ValueError:
        return None


def _int_or_zero(value) -> int:
    try:
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return 0


def _preview_stock(preview: dict | None) -> int:
    if not isinstance(preview, dict):
        return 0
    candidates = [
        (preview.get("global_product") or {}).get("available_quantity"),
        (preview.get("standard_box") or {}).get("available_quantity"),
        (preview.get("master_carton") or {}).get("available_quantity"),
    ]
    for value in candidates:
        qty = _int_or_zero(value)
        if qty > 0:
            return qty
    return 0


async def _fetch_price_preview(page, row) -> dict | None:
    variant_id = None
    variant_el = row.locator("[data-variant-id]").first
    if await variant_el.count() > 0:
        variant_id = await variant_el.get_attribute("data-variant-id")
    if not variant_id:
        data_id_el = row.locator("[data-id]").first
        if await data_id_el.count() > 0:
            variant_id = await data_id_el.get_attribute("data-id")
    if not variant_id:
        return None

    quantity = 100
    qty_input = row.locator('input[name="quantity"], input.productCartCell-quantity').first
    if await qty_input.count() > 0:
        raw_qty = await qty_input.get_attribute("value")
        quantity = _int_or_zero(raw_qty) or quantity

    try:
        response = await page.context.request.post(
            PRICE_PREVIEW_URL,
            data={"id": _int_or_zero(variant_id), "quantity": quantity},
        )
        if response.status != 200:
            log.warning("Vipa price-preview returned HTTP %s", response.status)
            return None
        return await response.json()
    except Exception as exc:
        log.warning("Vipa price-preview request failed: %s", exc)
        return None


async def _find_product_href(page, part_no: str) -> str | None:
    """Find the product URL from search results by raw or URL-encoded SKU."""
    raw = part_no.strip()
    encoded = quote(raw, safe="")
    try:
        await page.locator('a[href*="_sku="]').first.wait_for(timeout=10000)
    except PlaywrightTimeout:
        return None

    links = page.locator('a[href*="_sku="]')
    count = await links.count()
    fallback: str | None = None
    for idx in range(count):
        href = await links.nth(idx).get_attribute("href")
        if not href:
            continue
        fallback = fallback or href
        if raw in href or encoded in href:
            return href
    return fallback if count == 1 else None


async def _parse_product_table_price(page, part_no: str) -> tuple[float, int, int]:
    """
    Read price from the exact SKU row on the product page.

    Vipa renders the variant table first and loads the price through
    /en/j/price-preview/ after clicking "Discover Price". This is more stable
    than the details modal, which can remain in "Loading..." state headlessly.
    """
    row = page.locator("#variants tbody tr").filter(has_text=part_no).first
    try:
        await row.wait_for(timeout=15000)
    except PlaywrightTimeout as exc:
        raise RuntimeError(MSG_NOT_FOUND) from exc

    preview = await _fetch_price_preview(page, row)
    stock = _preview_stock(preview)

    discover_btn = row.locator('button:has-text("Discover Price"), button:has-text("Update price")').first
    if await discover_btn.count() > 0:
        await discover_btn.click()

    try:
        await page.wait_for_function(
            """(sku) => {
                const rows = [...document.querySelectorAll('#variants tbody tr')];
                const row = rows.find((r) => (r.innerText || '').includes(sku));
                return !!row && /[0-9 .,\\u00a0]+\\s*€\\s*\\/\\s*[0-9 .,\\u00a0]+/.test(row.innerText || '');
            }""",
            arg=part_no,
            timeout=15000,
        )
    except PlaywrightTimeout as exc:
        row_text = await row.inner_text(timeout=5000)
        parsed = _parse_price_per_qty(row_text)
        if not parsed:
            preview_price = (preview.get("global_product") or {}).get("net_price") if isinstance(preview, dict) else None
            if preview_price:
                return float(preview_price), 1000, stock
            raise RuntimeError(MSG_NOT_PRICED) from exc
        return parsed[0], parsed[1], stock or _parse_stock(row_text)

    row_text = await row.inner_text(timeout=5000)
    parsed = _parse_price_per_qty(row_text)
    if not parsed:
        preview_price = (preview.get("global_product") or {}).get("net_price") if isinstance(preview, dict) else None
        if preview_price:
            return float(preview_price), 1000, stock
        raise RuntimeError(MSG_NOT_PRICED)
    return parsed[0], parsed[1], stock or _parse_stock(row_text)


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    part_no = (supplier_part_no or "").strip()
    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, SESSION_MAX_AGE_HOURS))

    if not use_session:
        if session:
            invalidate_session(SESSION_FILE)
        raise RuntimeError(VIPA_OTP_REQUIRED)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=session["state"])
            log.info("Restored saved Vipa session (age < %s h)", SESSION_MAX_AGE_HOURS)
        except Exception as exc:
            log.warning("Could not restore Vipa session: %s", exc)
            invalidate_session(SESSION_FILE)
            raise RuntimeError(VIPA_OTP_REQUIRED) from exc

        page = await context.new_page()

        try:
            await emit("Opening vipafasteners.com…")
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)

            if not await _is_logged_in(page):
                log.info("Vipa session expired on server side")
                invalidate_session(SESSION_FILE)
                raise RuntimeError(VIPA_OTP_REQUIRED)

            await emit(f"Searching for {part_no} on Vipa…")
            await page.goto(
                SEARCH_URL.format(part_no=quote(part_no, safe="")),
                wait_until="domcontentloaded",
                timeout=20000,
            )

            # The search results page should show a product link with ?_sku=<part_no>
            product_href = await _find_product_href(page, part_no)
            if not product_href:
                raise RuntimeError(MSG_NOT_FOUND)

            product_url = urljoin(HOME_URL, product_href)

            await emit("Opening product details on Vipa…")
            await page.goto(product_url, wait_until="networkidle", timeout=30000)

            await emit("Reading availability and price from Vipa…")
            price_raw, price_unit_qty, stock = await _parse_product_table_price(page, part_no)

            if not has_numeric_price(str(price_raw)):
                raise RuntimeError(MSG_NOT_PRICED)

            log.info(
                "Vipa parsed — part=%s price=%s EUR/%s stock=%s url=%s",
                part_no, price_raw, price_unit_qty, stock, product_url,
            )
            return {
                "supplier_part_no": part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "EUR",
                "unit":             "db",
                "stock":            stock,
                "product_url":      product_url,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception("Unexpected error during Vipa scrape: %s", exc)
            raise RuntimeError(f"Vipa scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Vipa browser closed")
