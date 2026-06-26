"""
Playwright scraper for irontrade.hu

Session flow:
  1. Try to restore saved session from assets/sessions/irontrade_session.json
     - Skip restore if saved_at > 20 h old
  2. Verify session with a positive authenticated marker:
     look for 'Kijelentkezés' link that only appears for logged-in users
  3. If session missing / stale / invalid: full login, save new session with timestamp

Login flow (fallback):
  1. GET /bejelentkezes → fill #LoginEmail + #LoginPassword → submit
  2. Livewire may return 419 (CSRF expired) → "This page has expired" dialog appears
     → accept dialog → page reloads → fill and submit again
  3. Redirects to https://irontrade.hu/ on success

Search flow:
  4. /kereso?name={supplier_part_no}
  5. Click first product link → full product page
  6. Extract Nettó ár, Készlet
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from agent.runtime_config import get_runtime_env
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("irontrade")

LOGIN_URL    = "https://irontrade.hu/bejelentkezes"
SEARCH_URL   = "https://irontrade.hu/kereso?name={part_no}"
_SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "irontrade_session.json"
_SESSION_MAX_AGE_H = 20
_SEARCH_READY_TIMEOUT = 12000
_PRICE_READY_TIMEOUT = 15000

_JS_NEXT_SIBLING = """
(labelText) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount === 0 && el.textContent.trim() === labelText)
            return el.nextElementSibling?.textContent?.trim() ?? null;
    }
    return null;
}
"""

_JS_LABEL_VALUE_CANDIDATES = """
(labelText) => {
    const candidates = [];
    const add = (value) => {
        const text = (value || '').replace(/\\s+/g, ' ').trim();
        if (text && !candidates.includes(text)) candidates.push(text);
    };

    for (const el of document.querySelectorAll('*')) {
        if (el.childElementCount !== 0 || el.textContent.trim() !== labelText) continue;

        add(el.nextElementSibling?.textContent);

        let sibling = el.nextElementSibling;
        for (let i = 0; sibling && i < 5; i += 1, sibling = sibling.nextElementSibling) {
            add(sibling.textContent);
        }

        let parent = el.parentElement;
        for (let depth = 0; parent && depth < 3; depth += 1, parent = parent.parentElement) {
            const text = (parent.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text.includes(labelText)) continue;
            add(text.replace(labelText, ' ').trim());
        }
    }
    return candidates;
}
"""


def _looks_like_price(text: str | None) -> bool:
    return bool(text and has_numeric_price(text) and re.search(r"\b(?:Ft|HUF)\b", text, re.I))


def _search_url(supplier_part_no: str) -> str:
    return SEARCH_URL.format(part_no=quote(str(supplier_part_no), safe=""))


def _norm_part(value: str | None) -> str:
    """Loose part-number normalisation for exact-ish comparisons."""
    return re.sub(r"[\s._/-]+", "", str(value or "")).upper()


async def _extract_labeled_value(page, label: str) -> str | None:
    candidates = await page.evaluate(_JS_LABEL_VALUE_CANDIDATES, label)
    if not isinstance(candidates, list):
        candidates = []

    # Prefer concise values. Parent-block fallbacks are useful but often contain
    # extra labels, so they are only used if no direct sibling-style value exists.
    direct = await page.evaluate(_JS_NEXT_SIBLING, label)
    if direct:
        candidates.insert(0, direct)

    for candidate in candidates:
        text = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if text and label not in text:
            return text

    for candidate in candidates:
        text = re.sub(r"\s+", " ", str(candidate or "").replace(label, " ")).strip()
        if text:
            return text
    return None


async def _extract_price_value(page) -> str | None:
    from agent.tools import parse_price_string

    candidates = await page.evaluate(_JS_LABEL_VALUE_CANDIDATES, "Nettó ár:")
    if not isinstance(candidates, list):
        candidates = []

    direct = await page.evaluate(_JS_NEXT_SIBLING, "Nettó ár:")
    if direct:
        candidates.insert(0, direct)

    price_patterns = [
        r"\d[\d\s.]*,\d+\s*(?:Ft|HUF)\s*/\s*(?:\d[\d\s.,]*\s*)?\w+",
        r"\d[\d\s.]*\s*(?:Ft|HUF)\s*/\s*(?:\d[\d\s.,]*\s*)?\w+",
    ]

    for raw in candidates:
        text = re.sub(r"\s+", " ", str(raw or "").replace("Nettó ár:", " ")).strip()
        if not _looks_like_price(text):
            continue
        try:
            parse_price_string(text)
            return text
        except Exception:
            pass
        for pattern in price_patterns:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            candidate = match.group(0).strip()
            try:
                parse_price_string(candidate)
                return candidate
            except Exception:
                continue
    return None


async def _find_exact_result_row(page, supplier_part_no: str):
    wanted = _norm_part(supplier_part_no)
    rows = page.locator("table tbody tr")
    count = await rows.count()
    for idx in range(count):
        row = rows.nth(idx)
        try:
            sku_links = row.locator(f"a[data-sku='{supplier_part_no}']")
            if await sku_links.count():
                return row

            cells = row.locator("td")
            for cell_idx in range(await cells.count()):
                cell_text = (await cells.nth(cell_idx).inner_text()).strip()
                if _norm_part(cell_text) == wanted:
                    return row

            text = await row.inner_text()
        except Exception:
            continue
        if wanted and wanted in _norm_part(text):
            return row
    return None


async def _is_logged_in(page) -> bool:
    """
    Session check with negative and positive signals. URL redirect to the login
    page is the strongest negative signal; a visible login form is the fallback.
    """
    if "/bejelentkezes" in page.url:
        return False
    try:
        if await page.locator("#LoginEmail, #LoginPassword").first.is_visible(timeout=1000):
            return False
    except Exception:
        pass
    try:
        if await page.get_by_text("Kijelentkezés", exact=False).first.is_visible(timeout=1000):
            return True
    except Exception:
        pass
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
        body_lower = body_text.lower()
        if "üdvözöllek" in body_lower or "profilom" in body_lower:
            return True
        if "bejelentkezés" in body_lower or "regisztráció" in body_lower:
            return False
    except Exception:
        pass
    return False


async def _wait_for_login_success(page, timeout: int) -> None:
    """Wait for any reliable post-login signal instead of one exact landing URL."""
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            const body = document.body?.innerText || '';
            return !url.includes('/bejelentkezes')
                || body.includes('Kijelentkezés')
                || body.includes('Üdvözöllek')
                || body.includes('Profilom');
        }""",
        timeout=timeout,
    )
    if not await _is_logged_in(page):
        raise PlaywrightTimeout("Still on login page after submit")


def _attach_dialog(page) -> None:
    async def handle_dialog(dialog):
        log.info(f"Dialog dismissed: '{dialog.message}'")
        await dialog.accept()
    page.on("dialog", handle_dialog)


async def _login_or_restore(pw, emit: Callable, verify_url: str):
    """Launch a browser and return (browser, context, page) on a logged-in
    irontrade context. Verifies a fresh session against `verify_url`; on success
    `page` is left on that URL, otherwise it performs the full login (CSRF retry)
    and navigates to `verify_url`. Shared by both entrypoints."""
    browser = await pw.chromium.launch(headless=True)

    session  = load_session(_SESSION_FILE)
    context  = None
    skip_login = False

    if session and session_is_fresh(session, _SESSION_MAX_AGE_H):
        try:
            context = await browser.new_context(storage_state=session["state"])
            log.info("Session restored (age < 20 h) — will verify after navigation")
        except Exception as exc:
            log.warning(f"Could not restore session state: {exc}")
            invalidate_session(_SESSION_FILE)
            context = None
    elif session:
        log.info("Session stale (> 20 h) — proactive re-login")
        invalidate_session(_SESSION_FILE)

    if context is None:
        context = await browser.new_context()

    page = await context.new_page()
    _attach_dialog(page)

    await emit("Opening irontrade.hu…")

    # ── Step 2: verify session by navigating and checking auth ─
    if session and session_is_fresh(session, _SESSION_MAX_AGE_H):
        await page.goto(verify_url, wait_until="domcontentloaded", timeout=20000)
        if await _is_logged_in(page):
            log.info("Session valid — skipping login")
            skip_login = True
        else:
            log.warning("Session invalid — falling back to full login")
            invalidate_session(_SESSION_FILE)
            await context.close()
            context = await browser.new_context()
            page = await context.new_page()
            _attach_dialog(page)

    # ── Step 3: full login (if session missing or invalid) ────────────
    if not skip_login:
        await page.goto(LOGIN_URL, wait_until="load")
        log.info(f"Loaded login page: {page.url}")

        try:
            await page.get_by_role("button", name="Összes elfogadása").click(timeout=4000)
            await page.wait_for_timeout(800)
            log.info("Cookie banner accepted")
        except PlaywrightTimeout:
            log.info("No cookie banner appeared")

        async def fill_login_form():
            username = get_runtime_env("SUPPLIER_B_USERNAME", "")
            log.info(f"Filling login form for user: {username}")
            await page.locator("#LoginEmail").fill(username)
            await page.locator("#LoginPassword").fill(get_runtime_env("SUPPLIER_B_PASSWORD", ""))
            filled_email = await page.locator("#LoginEmail").input_value()
            filled_pass  = await page.locator("#LoginPassword").input_value()
            log.info(f"Form filled — email: {filled_email}, password length: {len(filled_pass)}")
            btn = page.get_by_role("button", name="Bejelentkezés")
            await btn.wait_for(state="visible", timeout=10000)
            await btn.evaluate("el => el.removeAttribute('disabled')")
            await btn.click()
            log.info("Login button clicked")

        await emit("Logging in to irontrade.hu…")
        await fill_login_form()

        try:
            await _wait_for_login_success(page, timeout=8000)
            log.info(f"Login successful on first attempt: {page.url}")

        except PlaywrightTimeout:
            log.warning(f"First login attempt timed out (likely CSRF dialog), URL: {page.url}")
            await page.wait_for_timeout(2500)
            log.info(f"URL after waiting for dialog reload: {page.url}")

            if await _is_logged_in(page):
                log.info("Login succeeded late after timeout")
            else:
                log.info("Retrying login with fresh CSRF token…")
                if "/bejelentkezes" not in page.url:
                    await page.goto(LOGIN_URL, wait_until="load", timeout=20000)
                if await _is_logged_in(page):
                    log.info("Login already active after reopening login page")
                else:
                    await fill_login_form()
                    try:
                        await _wait_for_login_success(page, timeout=12000)
                        log.info(f"Login successful on retry: {page.url}")
                    except PlaywrightTimeout:
                        current_url = page.url
                        log.error(f"Login still failed after retry — URL: {current_url}")
                        body_text = await page.locator("body").inner_text(timeout=5000)
                        for line in body_text.splitlines():
                            line = line.strip()
                            if line and any(w in line.lower() for w in ["hiba", "error", "sikertelen", "érvénytelen"]):
                                log.error(f"Page error text: {line}")
                        raise RuntimeError("Login to irontrade.hu failed. Please check credentials.")

        try:
            await save_session(context, _SESSION_FILE)
        except Exception as exc:
            log.warning(f"Could not save session: {exc}")

        # Navigate to the verify (search) URL after login
        log.info(f"Navigating to search URL: {verify_url}")
        await page.goto(verify_url, wait_until="domcontentloaded", timeout=20000)

    return browser, context, page


async def _search_and_parse(page, supplier_part_no: str, emit: Callable, navigate: bool = True) -> dict:
    """Search one part on a logged-in irontrade page and parse price + stock.
    When `navigate` is True it loads the part's search URL first; when False it
    assumes the page is already on that search result page."""
    await emit(f"Searching for part {supplier_part_no} on irontrade.hu…")
    if navigate:
        await page.goto(_search_url(supplier_part_no), wait_until="domcontentloaded", timeout=20000)

    log.info(f"Search page loaded: {page.url}")
    wait_tasks = [
        asyncio.create_task(page.wait_for_selector("table tbody tr", timeout=_SEARCH_READY_TIMEOUT)),
        asyncio.create_task(page.wait_for_selector("text=Találat: 0", timeout=_SEARCH_READY_TIMEOUT)),
    ]
    try:
        done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception:
        log.warning("No explicit search result marker appeared after 8s — continuing with body inspection")
    finally:
        for task in wait_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*wait_tasks, return_exceptions=True)

    body_text = await page.locator("body").inner_text(timeout=10000)
    log.info(f"Search page body (first 300): {body_text[:300]}")

    if "Találat: 0" in body_text:
        log.warning(f"Zero results for part {supplier_part_no}")
        raise RuntimeError(MSG_NOT_FOUND)

    rows = await page.locator("table tbody tr").count()
    log.info(f"Search result rows found: {rows}")

    if rows == 0:
        raise RuntimeError(MSG_NOT_FOUND)

    # ── Step 5: navigate to exact product page ───────────────────────
    exact_row = await _find_exact_result_row(page, supplier_part_no)
    if not exact_row:
        raise RuntimeError(MSG_NOT_FOUND)

    product_link = exact_row.locator(f"a[data-sku='{supplier_part_no}']").first
    if not await product_link.count():
        product_link = exact_row.locator("a").filter(has_text=supplier_part_no).first
    if not await product_link.count():
        product_link = exact_row.locator("a[href*='/termek'], a[href*='/product']").first
    if not await product_link.count():
        links = exact_row.locator("a")
        if await links.count() >= 2:
            product_link = links.nth(1)
        elif await links.count() == 1:
            product_link = links.first
    if not await product_link.count():
        raise RuntimeError(MSG_NOT_FOUND)

    link_text = (await product_link.inner_text()).strip() if await product_link.count() else ""
    href = await product_link.get_attribute("href")
    log.info(
        "Opening exact Irontrade result for %s (link text=%r, href=%r)",
        supplier_part_no,
        link_text,
        href,
    )
    if href:
        await page.goto(href, wait_until="domcontentloaded", timeout=20000)
    else:
        await product_link.click()
        await page.wait_for_load_state("domcontentloaded")
    log.info(f"Product page URL: {page.url}")

    try:
        product_body = await page.locator("body").inner_text(timeout=5000)
        if _norm_part(supplier_part_no) not in _norm_part(product_body):
            log.warning("Product page does not visibly contain requested part number: %s", supplier_part_no)
    except Exception:
        pass

    try:
        # Wait until the price *value* (sibling of "Nettó ár:") contains a digit.
        # The label appears immediately in static HTML; the value is populated later
        # by Livewire/AJAX — so waiting only for the label caused intermittent
        # not_priced errors when the value hadn't loaded yet.
        await page.wait_for_function(
            """() => {
                const addCandidates = (labelText) => {
                    const candidates = [];
                    for (const el of document.querySelectorAll('*')) {
                        if (el.childElementCount !== 0 || el.textContent.trim() !== labelText) continue;
                        if (el.nextElementSibling?.textContent) candidates.push(el.nextElementSibling.textContent);
                        let parent = el.parentElement;
                        for (let depth = 0; parent && depth < 3; depth += 1, parent = parent.parentElement) {
                            if ((parent.innerText || '').includes(labelText)) candidates.push(parent.innerText);
                        }
                    }
                    return candidates;
                };
                return addCandidates('Nettó ár:').some(text => /\\d/.test(text || ''));
            }""",
            timeout=_PRICE_READY_TIMEOUT,
        )
        log.info("Product page loaded — price value confirmed numeric")
    except PlaywrightTimeout:
        log.error(f"Price value not available on product page: {page.url}")
        raise RuntimeError(MSG_NOT_PRICED)

    # ── Step 6: extract price and stock ──────────────────────────────
    await emit("Reading price and stock from irontrade.hu…")
    price_str = await _extract_price_value(page)
    stock_str = await _extract_labeled_value(page, "Készlet:")

    log.info(f"Raw extracted values — price: '{price_str}', stock: '{stock_str}'")

    if not price_str or not has_numeric_price(price_str):
        # Product page is open, but the price is missing or shown as text.
        raise RuntimeError(MSG_NOT_PRICED)

    from agent.tools import parse_price_string, parse_stock_string
    price_raw, price_unit_qty, unit = parse_price_string(price_str)
    log.info(f"Parsed price: {price_raw} HUF / {price_unit_qty} {unit}")

    result = {
        "supplier_part_no": supplier_part_no,
        "price_raw":        price_raw,
        "price_unit_qty":   price_unit_qty,
        "currency":         "HUF",
        "unit":             unit,
        "stock":            parse_stock_string(stock_str) if stock_str else 0,
        "queried_at":       datetime.now().isoformat(timespec="seconds"),
    }
    log.info(f"Final result: {result}")
    return result


# ── Single lookup (price agent) ──────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    search_url = _search_url(supplier_part_no)
    async with async_playwright() as pw:
        browser, context, page = await _login_or_restore(pw, emit, search_url)
        try:
            # `page` is already on this part's search result page.
            return await _search_and_parse(page, supplier_part_no, emit, navigate=False)
        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during irontrade.hu scrape: {exc}")
            raise RuntimeError(f"irontrade.hu scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")


# ── Batch lookup (batch agent) ───────────────────────────────────────────────

async def fetch_prices(
    part_nos: list[str],
    on_progress: Callable | None = None,
    on_item: Callable | None = None,
) -> list[dict]:
    """Look up several parts in ONE irontrade session (login once, reuse browser)."""
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    results: list[dict] = []
    if not part_nos:
        return results

    total = len(part_nos)
    first_url = _search_url(part_nos[0])

    async with async_playwright() as pw:
        browser, context, page = await _login_or_restore(pw, emit, first_url)
        try:
            for i, pn in enumerate(part_nos):
                try:
                    r = await _search_and_parse(page, pn, emit, navigate=True)
                    results.append(r)
                    if on_item:
                        await on_item(i, total, pn, r, None)
                except RuntimeError as exc:
                    msg = str(exc)
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
                except Exception as exc:
                    log.exception(f"Unexpected error during irontrade.hu scrape ({pn}): {exc}")
                    msg = f"irontrade.hu scrape failed: {exc}"
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
        finally:
            await browser.close()
            log.info("Browser closed")

    return results
