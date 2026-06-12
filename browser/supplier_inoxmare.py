"""Playwright scraper for www.inoxmare.com (Supplier L).

The authenticated homepage has two search fields. ``#item-input`` is the exact
Inoxmare/customer-code resolver; submitting it redirects to the matching
product-family page with ``?art=<part_no>`` and highlights the exact table row.

The displayed standard price is sent by the webshop's own cart JavaScript as
Magento ``custom_price`` for the entered piece quantity, so it is a per-piece
EUR price.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh

load_dotenv()

log = logging.getLogger("inoxmare")

HOME_URL = "https://www.inoxmare.com/en"
LOGIN_URL = "https://www.inoxmare.com/en/customer/account/login/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "inoxmare_session.json"
SESSION_MAX_AGE_HOURS = 20


async def _is_logged_in(page) -> bool:
    try:
        body_text = await page.locator("body").inner_text()
        has_account_state = "Sign Out" in body_text or "Welcome," in body_text
        return has_account_state and await page.locator("#item-input").count() > 0
    except Exception:
        return False


async def _login(page) -> None:
    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    username = os.getenv("SUPPLIER_L_USERNAME", "")
    password = os.getenv("SUPPLIER_L_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Login to inoxmare.com failed. Please check credentials.")

    await page.locator('a:has-text("Sign In")').first.click()
    await page.locator("#email:visible").fill(username)
    await page.locator("#pass:visible").fill(password)
    await page.locator("#send2:visible").click()
    try:
        await page.wait_for_function(
            """() => {
                const body = document.body.innerText;
                return (body.includes('Sign Out') || body.includes('Welcome,'))
                    && !!document.querySelector('#item-input');
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout as exc:
        raise RuntimeError("Login to inoxmare.com failed. Please check credentials.") from exc


def _parse_stock(text: str) -> int:
    cleaned = (text or "").strip()
    if not cleaned or cleaned == "!":
        return 0
    groups = re.findall(r"\d+", cleaned)
    return int("".join(groups)) if groups else 0


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    part_no = (supplier_part_no or "").strip()
    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, SESSION_MAX_AGE_HOURS))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        if use_session:
            try:
                context = await browser.new_context(storage_state=session["state"])
                log.info("Restored saved Inoxmare session (age < 20 h)")
            except Exception as exc:
                log.warning("Could not restore Inoxmare session: %s", exc)
                invalidate_session(SESSION_FILE)
                use_session = False
                context = await browser.new_context()
        else:
            if session:
                invalidate_session(SESSION_FILE)
            context = await browser.new_context()
        page = await context.new_page()

        try:
            await emit("Opening inoxmare.com…")
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)

            if use_session and not await _is_logged_in(page):
                log.info("Saved Inoxmare session expired — logging in again")
                invalidate_session(SESSION_FILE)
                await context.close()
                context = await browser.new_context()
                page = await context.new_page()
                use_session = False

            if not use_session:
                await emit("Logging in to inoxmare.com…")
                await _login(page)
                try:
                    await save_session(context, SESSION_FILE)
                except Exception as exc:
                    log.warning("Could not persist Inoxmare session: %s", exc)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)

            await emit(f"Searching for {part_no} on Inoxmare…")
            exact_search = page.locator("#item-input:visible")
            await exact_search.wait_for(timeout=10000)
            await exact_search.fill(part_no)
            await exact_search.press("Enter")

            try:
                await page.wait_for_function(
                    """partNo => {
                        const row = document.querySelector(`tr[id="${CSS.escape(partNo)}"]`);
                        return new URLSearchParams(location.search).get('art') === partNo && !!row;
                    }""",
                    arg=part_no,
                    timeout=20000,
                )
            except PlaywrightTimeout as exc:
                raise RuntimeError(MSG_NOT_FOUND) from exc

            row = page.locator(f'tr[id="{part_no}"]')
            if await row.count() != 1:
                raise RuntimeError(MSG_NOT_FOUND)

            await emit("Reading price and stock from Inoxmare…")
            price_input = row.locator("td.price-box input[id$='-custom-price']").first
            price_text = await price_input.get_attribute("value")
            if not price_text or not has_numeric_price(price_text):
                raise RuntimeError(MSG_NOT_PRICED)
            price_raw = float(price_text.replace(",", "."))

            stock_text = await row.locator("td.stock").inner_text()
            stock = _parse_stock(stock_text)

            description = ""
            description_el = row.locator("td.descr p").first
            if await description_el.count():
                description = (await description_el.inner_text()).strip()

            product_url = page.url
            log.info(
                "Inoxmare parsed — part=%s, price=%s EUR/db, stock=%s, url=%s",
                part_no,
                price_raw,
                stock,
                product_url,
            )
            return {
                "supplier_part_no": part_no,
                "product_name": description,
                "price_raw": price_raw,
                "price_unit_qty": 1,
                "currency": "EUR",
                "unit": "db",
                "stock": stock,
                "product_url": product_url,
                "queried_at": datetime.now().isoformat(timespec="seconds"),
            }
        except RuntimeError:
            raise
        except Exception as exc:
            log.exception("Unexpected error during Inoxmare scrape: %s", exc)
            raise RuntimeError(f"Inoxmare scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Inoxmare browser closed")
