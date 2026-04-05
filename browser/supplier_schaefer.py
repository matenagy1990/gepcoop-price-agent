"""
Playwright scraper for shop.schaefer-peters.com (Supplier I)

Login flow:
  1. GET /sp/en/login/ → fill input[name='input_login'] + input[name='input_password']
     → click "Log in" → redirects to /b2b/en/?action=shop_login

Search flow:
  2. Open the authenticated shop homepage, fill the visible search field with the
     article number and press Enter
     → exact article numbers usually navigate directly to
       /b2b/en/art-{slug}-p{id}/
  3. If the shop returns a search results page (/b2b/en/search/), look for an
     exact part-number row in the main content and only then open the product link

Data extraction:
  - Price:    span[itemprop='price'] content attribute → clean float (e.g. "4.58")
  - Unit qty: .priceLabel text → regex for number before "Pcs." (e.g. "Price 100 Pcs.")
  - Stock:    .inventory p text → strip thousand-separating dots → int (e.g. "50.800 Pcs." → 50800)

Currency: EUR
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

log = logging.getLogger("schaefer")

LOGIN_URL = "https://shop.schaefer-peters.com/sp/en/login/"
HOME_URL = "https://shop.schaefer-peters.com/b2b/en/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "schaefer_session.json"
SESSION_MAX_AGE_HOURS = 20


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _contains_exact_part(text: str, supplier_part_no: str) -> bool:
    text_n = _normalize_text(text)
    part_n = _normalize_text(supplier_part_no)
    if not text_n or not part_n:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(part_n)}(?![A-Za-z0-9])"
    return re.search(pattern, text_n) is not None


async def _is_logged_in(page) -> bool:
    if "/login" in page.url:
        return False
    try:
        return await page.evaluate(
            """() => {
                const body = document.body?.innerText || '';
                return !!document.querySelector("input[type='search']") && /logout/i.test(body);
            }"""
        )
    except Exception:
        return False


def _has_no_results(body_text: str) -> bool:
    low = body_text.lower()
    return "no entries found" in low or "no result" in low or "0 article" in low


async def _get_visible_search_input(page):
    visible = page.locator("input[type='search']:visible")
    if await visible.count():
        return visible.first
    return page.locator("input[type='search']").first


async def _search_from_home(page, supplier_part_no: str) -> None:
    search_input = await _get_visible_search_input(page)
    await search_input.wait_for(state="visible", timeout=10000)
    await search_input.fill("")
    await search_input.fill(supplier_part_no)
    filled = await search_input.input_value()
    if filled != supplier_part_no:
        await search_input.press("ControlOrMeta+A")
        await search_input.press("Backspace")
        await search_input.type(supplier_part_no, delay=25)
    await search_input.press("Enter")


async def _open_exact_result_from_search(page, supplier_part_no: str) -> bool:
    main = page.locator("main").first if await page.locator("main").count() else page.locator("body").first

    table_rows = main.locator("table tr")
    table_row_count = await table_rows.count()
    for idx in range(table_row_count):
        row = table_rows.nth(idx)
        try:
            row_text = await row.inner_text()
        except Exception:
            continue
        if not _contains_exact_part(row_text, supplier_part_no):
            continue
        link = row.locator("a[href*='/b2b/en/art-']")
        if await link.count():
            await link.first.click(timeout=8000)
            return True

    links = main.locator("a[href*='/b2b/en/art-']")
    link_count = await links.count()
    for idx in range(link_count):
        link = links.nth(idx)
        try:
            link_text = await link.inner_text()
        except Exception:
            continue
        if _contains_exact_part(link_text, supplier_part_no):
            await link.click(timeout=8000)
            return True

    exact_rows = main.locator("tr")
    row_count = await exact_rows.count()
    for idx in range(row_count):
        row = exact_rows.nth(idx)
        try:
            row_text = await row.inner_text()
        except Exception:
            continue
        if not _contains_exact_part(row_text, supplier_part_no):
            continue
        link = row.locator("a[href*='/b2b/en/art-']")
        if await link.count():
            await link.first.click(timeout=8000)
            return True

    exact_blocks = main.locator("article, li, div")
    block_count = min(await exact_blocks.count(), 12)
    for idx in range(block_count):
        block = exact_blocks.nth(idx)
        try:
            block_text = await block.inner_text()
        except Exception:
            continue
        if not _contains_exact_part(block_text, supplier_part_no):
            continue
        link = block.locator("a[href*='/b2b/en/art-']")
        if await link.count():
            await link.first.click(timeout=8000)
            return True

    return False


async def _is_product_page(page, supplier_part_no: str) -> bool:
    try:
        if await page.locator("span[itemprop='price']").count():
            body_text = await page.locator("body").inner_text()
            return _contains_exact_part(body_text, supplier_part_no)
    except Exception:
        return False
    return False


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, SESSION_MAX_AGE_HOURS))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        if use_session:
            try:
                ctx = await browser.new_context(storage_state=session["state"])
                log.info("Restored saved session (age < 20 h)")
            except Exception as exc:
                log.warning(f"Could not restore saved session: {exc}")
                invalidate_session(SESSION_FILE)
                use_session = False
                ctx = await browser.new_context()
        else:
            if session:
                log.info("Session stale (> 20 h) — proactive re-login")
                invalidate_session(SESSION_FILE)
            ctx = await browser.new_context()
        page = await ctx.new_page()

        try:
            await emit("Opening shop.schaefer-peters.com…")

            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                if not await _is_logged_in(page):
                    log.warning("Saved session is no longer valid — performing fresh login")
                    invalidate_session(SESSION_FILE)
                    await ctx.close()
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    use_session = False
                else:
                    log.info("Session valid — login skipped")

            if not use_session:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                log.info(f"Login page: {page.url}")

                await emit("Logging in to shop.schaefer-peters.com…")
                username = os.getenv("SUPPLIER_I_USERNAME", "")
                password = os.getenv("SUPPLIER_I_PASSWORD", "")
                log.info(f"Logging in as: {username}")

                await page.locator("input[name='input_login']").first.fill(username)
                await page.locator("input[name='input_password']").first.fill(password)
                await page.locator("button:has-text('Log in')").first.click()

                try:
                    await page.wait_for_function(
                        "() => !location.pathname.includes('/login') && !!document.querySelector(\"input[type='search']\") && /logout/i.test(document.body.innerText)",
                        timeout=12000,
                    )
                except PlaywrightTimeout:
                    raise RuntimeError(
                        "Login to shop.schaefer-peters.com failed. Please check credentials."
                    )
                log.info(f"Login successful: {page.url}")
                try:
                    await save_session(ctx, SESSION_FILE)
                except Exception as exc:
                    log.warning(f"Could not persist session: {exc}")

                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)

            # Search — use the visible search box from the authenticated homepage
            await emit(f"Searching for {supplier_part_no} on schaefer-peters…")
            await _search_from_home(page, supplier_part_no)
            try:
                await page.wait_for_function(
                    "() => location.pathname.includes('/b2b/en/search/') || !!document.querySelector(\"span[itemprop='price']\")",
                    timeout=12000,
                )
            except PlaywrightTimeout:
                body_text = await page.locator("body").inner_text()
                if _has_no_results(body_text):
                    raise RuntimeError(f"Part {supplier_part_no} was not found on schaefer-peters.")
                raise RuntimeError(
                    f"Search for {supplier_part_no} did not resolve to a product or results page on schaefer-peters."
                )
            log.info(f"After search: {page.url}")

            # If still on a search results page, open only an exact matching result
            if "/search/" in page.url and not await _is_product_page(page, supplier_part_no):
                body_text = await page.locator("body").inner_text()
                if _has_no_results(body_text):
                    raise RuntimeError(
                        f"Part {supplier_part_no} was not found on schaefer-peters."
                    )
                try:
                    await page.wait_for_function(
                        "() => !!document.querySelector('table a[href*=\"/b2b/en/art-\"]') || /no entries found|no result|0 article/i.test(document.body.innerText)",
                        timeout=8000,
                    )
                    body_text = await page.locator("body").inner_text()
                    if _has_no_results(body_text):
                        raise RuntimeError(
                            f"Part {supplier_part_no} was not found on schaefer-peters."
                        )
                    opened = await _open_exact_result_from_search(page, supplier_part_no)
                    if not opened:
                        raise RuntimeError(
                            f"Search results loaded for {supplier_part_no}, but no exact result row could be identified on schaefer-peters."
                        )
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_selector("span[itemprop='price']", timeout=8000)
                    log.info(f"Product page: {page.url}")
                except PlaywrightTimeout:
                    raise RuntimeError(
                        f"Search results for {supplier_part_no} did not finish loading on schaefer-peters."
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
