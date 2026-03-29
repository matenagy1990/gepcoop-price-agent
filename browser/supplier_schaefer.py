"""
Playwright scraper for shop.schaefer-peters.com (Supplier I)

Login flow:
  1. GET /sp/en/login/ → fill input[name='input_login'] + input[name='input_password']
     → click "Log in" → redirects to /b2b/en/?action=shop_login

Search flow:
  2. Fill input[type='search'] with the article number → press Enter
     → navigates directly to the product page /b2b/en/art-{slug}-p{id}/
     (for exact article numbers the shop always resolves to the product page)
  3. If landed on a search results page (/b2b/en/search/), click first /b2b/en/art- link

Data extraction:
  - Price:    span[itemprop='price'] content attribute → clean float (e.g. "4.58")
  - Unit qty: .priceLabel text → regex for number before "Pcs." (e.g. "Price 100 Pcs.")
  - Stock:    .inventory p text → strip thousand-separating dots → int (e.g. "50.800 Pcs." → 50800)

Currency: EUR
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

log = logging.getLogger("schaefer")

LOGIN_URL    = "https://shop.schaefer-peters.com/sp/en/login/"
HOME_URL     = "https://shop.schaefer-peters.com/b2b/en/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "schaefer_session.json"


def _load_saved_cookies() -> list | None:
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


def _save_cookies(cookies: list) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(cookies, indent=2))
        log.info(f"Session saved to {SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _is_logged_in(page) -> bool:
    return "/login" not in page.url


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def _do_login():
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            username = os.getenv("SUPPLIER_I_USERNAME", "")
            password = os.getenv("SUPPLIER_I_PASSWORD", "")
            log.info(f"Logging in as: {username}")
            await page.locator("input[name='input_login']").first.fill(username)
            await page.locator("input[name='input_password']").first.fill(password)
            await page.locator("button:has-text('Log in')").first.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
            if "/login" in page.url and "action=shop_login" not in page.url:
                raise RuntimeError(
                    "Login to shop.schaefer-peters.com failed. Please check credentials."
                )
            log.info(f"Login successful: {page.url}")

        try:
            await emit("Opening shop.schaefer-peters.com…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await ctx.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to shop.schaefer-peters.com…")
                    await _do_login()
                    _save_cookies(await ctx.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to shop.schaefer-peters.com…")
                await _do_login()
                _save_cookies(await ctx.cookies())

            # Search — use the search box and press Enter
            await emit(f"Searching for {supplier_part_no} on schaefer-peters…")
            search_box = page.locator("input[type='search']").first
            await search_box.fill(supplier_part_no)
            await search_box.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
            log.info(f"After search: {page.url}")

            # If still on a search results page, click the first product link
            if "/search/" in page.url or "/b2b/en/art-" not in page.url:
                body_text = await page.locator("body").inner_text()
                if "no result" in body_text.lower() or "0 article" in body_text.lower():
                    raise RuntimeError(
                        f"Part {supplier_part_no} was not found on schaefer-peters."
                    )
                try:
                    first_link = page.locator("a[href*='/b2b/en/art-']").first
                    await first_link.click(timeout=8000)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(1500)
                    log.info(f"Product page: {page.url}")
                except PlaywrightTimeout:
                    raise RuntimeError(
                        f"No product links found for {supplier_part_no} on schaefer-peters."
                    )

            await emit("Reading price and stock from schaefer-peters…")
            log.info(f"Extracting from: {page.url}")

            # --- Price via machine-readable itemprop ---
            price_el = page.locator("span[itemprop='price']")
            price_content = await price_el.get_attribute("content", timeout=8000)
            if not price_content:
                raise RuntimeError(
                    "Could not read price from schaefer-peters. Page layout may have changed."
                )
            price_raw = float(price_content)
            log.info(f"Price (itemprop): {price_raw} EUR")

            # --- Unit qty from .priceLabel: "Price 100 Pcs." ---
            try:
                label_text = await page.locator(".priceLabel").inner_text(timeout=5000)
                log.info(f"Price label: {label_text!r}")
                qty_match = re.search(r"(\d[\d.]*)\s*Pcs", label_text, re.IGNORECASE)
                price_unit_qty = int(qty_match.group(1).replace(".", "")) if qty_match else 1
            except Exception:
                price_unit_qty = 1
                log.warning("Could not read unit qty from .priceLabel, defaulting to 1")

            # --- Stock from .inventory p ---
            stock_value = 0
            try:
                inventory_el = page.locator("[class*='inventory']").filter(
                    has=page.locator("p")
                ).first
                stock_text = await inventory_el.locator("p").first.inner_text(timeout=5000)
                stock_text = stock_text.strip()
                log.info(f"Stock text: {stock_text!r}")
                # "50.800 Pcs." — dot is German thousands separator
                m = re.search(r"[\d.]+", stock_text)
                if m:
                    stock_value = int(m.group().replace(".", ""))
            except Exception as e:
                log.warning(f"Could not read stock: {e}")

            log.info(f"Parsed — {price_raw} EUR / {price_unit_qty} pcs, stock: {stock_value}")

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
            log.exception(f"Unexpected error during schaefer-peters scrape: {exc}")
            raise RuntimeError(f"schaefer-peters scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
