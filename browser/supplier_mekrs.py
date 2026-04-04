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

log = logging.getLogger("mekrs")

HOME_URL = "https://eshop.mekrs.cz/en"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "mekrs_session.json"
_SESSION_MAX_AGE_H = 20


def _parse_stock(s: str) -> int:
    """Extract stock in pcs, preferring the explicit pcs quantity when packaging is also shown."""
    if not s:
        return 0
    pcs_match = re.search(r"([\d.,]+)\s*pcs", s, flags=re.IGNORECASE)
    if pcs_match:
        return int(re.sub(r"[^\d]", "", pcs_match.group(1)) or "0")
    digit_groups = re.findall(r"\d+", s)
    return int("".join(digit_groups)) if digit_groups else 0


def _parse_czk_price(price_str: str) -> float:
    """Parse CZK price from either '50.63 Kč' or '50,63 Kč'."""
    clean = re.sub(r"[^\d.,]", "", price_str).strip()
    if not clean:
        raise ValueError("Empty CZK price string")
    if "," in clean and "." in clean:
        clean = clean.replace(",", "")
    elif "," in clean:
        clean = clean.replace(",", ".")
    return float(clean)


async def _is_search_visible(page) -> bool:
    return await page.locator("input[placeholder='Search by name, code, DIN']").first.is_visible()


async def _is_login_visible(page) -> bool:
    return await page.locator("input[name='username']").first.is_visible()


async def _wait_for_auth_state(page, timeout_ms: int = 12000) -> str:
    """Return 'search', 'login', or 'unknown' based on the visible post-login UI."""
    try:
        await page.wait_for_function(
            """() => {
                const search = document.querySelector("input[placeholder='Search by name, code, DIN']");
                const login = document.querySelector("input[name='username']");
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                };
                return visible(search) || visible(login);
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeout:
        return "unknown"

    if await _is_search_visible(page):
        return "search"
    if await _is_login_visible(page):
        return "login"
    return "unknown"


async def _capture_auth_signals(page) -> dict:
    data = await page.evaluate(
        """() => {
            const visible = (selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                const s = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const txt = document.body.innerText || '';
            return {
                login_visible: visible("input[name='username']"),
                login_button_visible: visible("[data-testid='login-button']"),
                search_visible: visible("input[placeholder='Search by name, code, DIN']"),
                body_has_login_prompt: txt.includes('To add an item to the cart, you need to log in'),
                body_has_without_vat: txt.includes('without VAT'),
                body_has_with_vat: txt.includes('with VAT'),
                body_snippet: txt.slice(0, 1200),
            };
        }"""
    )
    log.info(
        "Mekrs auth signals: login_visible=%r, login_button_visible=%r, search_visible=%r, "
        "login_prompt=%r, without_vat=%r, with_vat=%r",
        data["login_visible"],
        data["login_button_visible"],
        data["search_visible"],
        data["body_has_login_prompt"],
        data["body_has_without_vat"],
        data["body_has_with_vat"],
    )
    return data


async def _extract_price_block(container):
    return await container.evaluate("""(root) => {
        const leaves = Array.from(root.querySelectorAll('*'))
            .filter(el => el.childElementCount === 0)
            .map(el => ({ el, text: (el.textContent || '').trim() }))
            .filter(item => item.text);

        const isPrice = (text) => /[\\d][\\d.,]*\\s*Kč/.test(text);
        const isPcsUnit = (text) => /\\/\\s*\\d[\\d,]*\\s*pcs/i.test(text);

        const pairs = [];
        for (let i = 0; i < leaves.length; i++) {
            if (!isPrice(leaves[i].text)) continue;
            for (let j = i + 1; j <= Math.min(i + 4, leaves.length - 1); j++) {
                if (!isPcsUnit(leaves[j].text)) continue;
                const qtyMatch = leaves[j].text.match(/\\/\\s*(\\d[\\d,]*)\\s*pcs/i);
                const qty = qtyMatch ? parseInt(qtyMatch[1].replace(/,/g, ''), 10) : 0;
                pairs.push({
                    price: leaves[i].text,
                    unit: leaves[j].text,
                    html: leaves[i].el.outerHTML,
                    qty,
                });
                break;
            }
        }

        if (!pairs.length) return [null, null, null];

        pairs.sort((a, b) => b.qty - a.qty);
        return [pairs[0].price, pairs[0].unit, pairs[0].html];
    }""")


async def _extract_results_page_data(page):
    """Extract the first priced B2B result from the authenticated results page text."""
    return await page.evaluate(
        """() => {
            const body = (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
            const stockMatch = body.match(/In stock\\s+([\\d.,\\s]+)\\s*pcs/i);
            const unitPriceMatch = body.match(/([\\d][\\d.,]*)\\s*Kč\\s*\\/\\s*(\\d[\\d,]*)\\s*pcs\\s*without VAT/i);
            if (!unitPriceMatch) return null;

            return {
                price_str: `${unitPriceMatch[1]} Kč`,
                unit_str: `/ ${unitPriceMatch[2]} pcs`,
                price_html: null,
                stock_str: stockMatch ? `In stock ${stockMatch[1]} pcs` : '',
                block_html: body.slice(0, 3000),
                text: body.slice(0, 3000),
            };
        }"""
    )


async def _extract_detail_data(page, supplier_part_no: str):
    """Extract price and qty from the checked Mekrs variant row on detail pages."""
    return await page.evaluate(
        """(partNo) => {
            const checked = document.querySelector('input[name="variant"]:checked');
            const row = checked ? checked.closest('.relative') || checked.parentElement : null;
            if (!row) return null;

            const text = (row.textContent || '').trim().replace(/\\s+/g, ' ');
            const title = (row.querySelector('h3')?.textContent || '').trim();
            const stockText = (row.querySelector('div.text-sm.font-medium.text-primaryGreen')?.textContent || '').trim();

            const packagingQtyMatch = title.match(/\\((\\d[\\d,]*)\\s*pcs\\)/i);
            const packagingQty = packagingQtyMatch ? parseInt(packagingQtyMatch[1].replace(/,/g, ''), 10) : null;

            const lines = Array.from(row.querySelectorAll('div.grid.grid-flow-col.items-baseline'))
                .map(line => {
                    const priceNode = Array.from(line.querySelectorAll('*'))
                        .find(node => /[\\d][\\d.,]*\\s*Kč/.test((node.textContent || '').trim()));
                    const unitNode = Array.from(line.querySelectorAll('*'))
                        .find(node => /\\/\\s*(packaging|pcs)/i.test((node.textContent || '').trim()));
                    return {
                        price: priceNode ? (priceNode.textContent || '').trim() : null,
                        unit: unitNode ? (unitNode.textContent || '').trim() : null,
                        html: priceNode ? priceNode.outerHTML : null,
                    };
                })
                .filter(item => item.price && item.unit);

            const packagingLine = lines.find(item => /\\/\\s*packaging/i.test(item.unit));
            const pcsLine = lines.find(item => /\\/\\s*pcs/i.test(item.unit));

            return {
                part_present_on_page: document.body.innerText.includes(partNo),
                title,
                stock_str: stockText,
                packaging_qty: packagingQty,
                packaging_price_str: packagingLine ? packagingLine.price : null,
                pcs_price_str: pcsLine ? pcsLine.price : null,
                pcs_unit_str: pcsLine ? pcsLine.unit : null,
                price_html: packagingLine?.html || pcsLine?.html || null,
                block_html: row.innerHTML.slice(0, 3000),
            };
        }""",
        supplier_part_no,
    )


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

        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            await emit("Opening eshop.mekrs.cz…")

            # ── Step 1: try session restore ───────────────────────────────────
            if use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                state = await _wait_for_auth_state(page, timeout_ms=8000)
                signals = await _capture_auth_signals(page)
                log.info(f"After session restore, URL: {page.url}")

                if state == "login" or signals["login_visible"] or signals["login_button_visible"]:
                    log.warning("Session invalid (login form visible) — falling back to full login")
                    use_session = False
                    invalidate_session(_SESSION_FILE)
                    await browser.close()
                    browser  = await pw.chromium.launch(headless=True)
                    context  = await browser.new_context()
                    page     = await context.new_page()
                    page.on("dialog", handle_dialog)
                elif state == "search":
                    log.info("Session valid — login skipped")
                else:
                    log.warning("Session restore ended in unknown state — retrying home page before login fallback")
                    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                    state = await _wait_for_auth_state(page, timeout_ms=8000)
                    if state != "search":
                        use_session = False
                        invalidate_session(_SESSION_FILE)
                        await browser.close()
                        browser  = await pw.chromium.launch(headless=True)
                        context  = await browser.new_context()
                        page     = await context.new_page()
                        page.on("dialog", handle_dialog)
                    else:
                        log.info("Session valid after retry — login skipped")

            # ── Step 2: full login (if needed) ───────────────────────────────
            if not use_session:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await page.wait_for_selector("input[name='username']", timeout=8000)
                log.info(f"Loaded: {page.url}")

                await emit("Logging in to eshop.mekrs.cz…")
                username = os.getenv("SUPPLIER_D_USERNAME", "")
                log.info(f"Logging in as: {username}")

                user_input = page.locator("input[name='username']")
                pass_input = page.locator("input[name='password']")
                await user_input.click()
                await user_input.fill("")
                await user_input.type(username, delay=20)
                await pass_input.click()
                await pass_input.fill("")
                await pass_input.type(os.getenv("SUPPLIER_D_PASSWORD", ""), delay=20)
                log.info("Submitting Mekrs login with Enter on password field")
                await pass_input.press("Enter")
                await page.wait_for_timeout(3000)
                state = await _wait_for_auth_state(page, timeout_ms=12000)
                signals = await _capture_auth_signals(page)
                if state == "unknown":
                    log.warning("Login did not resolve to search/login state — retrying homepage")
                    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
                    state = await _wait_for_auth_state(page, timeout_ms=8000)
                    signals = await _capture_auth_signals(page)

                if state == "login" or signals["login_visible"] or signals["login_button_visible"]:
                    raise RuntimeError(
                        "Login to eshop.mekrs.cz did not establish an authenticated session."
                    )
                if state != "search":
                    raise RuntimeError(
                        "eshop.mekrs.cz login completed, but the search page did not become available."
                    )

                log.info(f"Login successful — URL: {page.url}")
                await save_session(context, _SESSION_FILE)

            # ── Step 3: search via autocomplete ──────────────────────────────
            await emit(f"Searching for part {supplier_part_no} on eshop.mekrs.cz…")
            search_inp = page.locator("input[placeholder='Search by name, code, DIN']").first
            await search_inp.wait_for(state="visible", timeout=8000)
            await search_inp.click()
            await search_inp.fill("")
            try:
                await page.wait_for_function(
                    "() => { const el = document.querySelector(\"input[placeholder='Search by name, code, DIN']\"); return !!el && el.value === ''; }",
                    timeout=3000,
                )
            except PlaywrightTimeout:
                log.warning("Mekrs search box did not clear via fill(''); trying select-all fallback")
                await search_inp.press("Control+A")
                await search_inp.press("Backspace")
            await search_inp.type(supplier_part_no, delay=50)
            log.info(f"Typed '{supplier_part_no}' into search box, waiting for Mekrs search suggestions…")
            try:
                await page.wait_for_function(
                    """(partNo) => {
                        const items = Array.from(document.querySelectorAll('li, [role="option"], a, button'));
                        return items.some(el => (el.textContent || '').includes(partNo)) ||
                               items.some(el => (el.textContent || '').includes('Show all results'));
                    }""",
                    arg=supplier_part_no,
                    timeout=8000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(autocomplete returned no results)."
                )

            current_value = await search_inp.input_value()
            if current_value != supplier_part_no:
                log.warning(
                    "Mekrs search input drifted from %r to %r; correcting before submit",
                    supplier_part_no,
                    current_value,
                )
                await search_inp.fill(supplier_part_no)
                await page.wait_for_function(
                    """(expected) => {
                        const el = document.querySelector("input[placeholder='Search by name, code, DIN']");
                        return !!el && el.value === expected;
                    }""",
                    arg=supplier_part_no,
                    timeout=3000,
                )

            log.info("Submitting Mekrs product search with Enter")
            await search_inp.press("Enter")

            try:
                await page.wait_for_function(
                    "() => location.pathname.includes('/products') && document.body.innerText.includes('Kč')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                raise RuntimeError(f"Part {supplier_part_no} was not found on eshop.mekrs.cz.")
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('without VAT')",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                log.warning("Mekrs results page did not show 'without VAT' in time")
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('Kč')",
                    timeout=10000,
                )
                log.info("Kč price text detected in DOM")
            except PlaywrightTimeout:
                log.warning("Kč not found in DOM after 10s — attempting extraction anyway")
            log.info(f"Results page loaded: {page.url}")
            signals = await _capture_auth_signals(page)
            if signals["body_has_login_prompt"] or (
                signals["body_has_with_vat"] and not signals["body_has_without_vat"]
            ):
                raise RuntimeError(
                    "eshop.mekrs.cz is showing public prices only; authenticated B2B pricing is not active."
                )

            cards = page.locator("[data-testid='product-card']")
            card_count = await cards.count()
            log.info(f"Product cards on results page: {card_count}")

            # ── Step 4: extract data ──────────────────────────────────────────
            await emit("Reading price and stock from eshop.mekrs.cz…")
            results_data = await _extract_results_page_data(page)
            if not results_data:
                raise RuntimeError(
                    f"Part {supplier_part_no} was not found on eshop.mekrs.cz "
                    "(authenticated results page did not expose a priced result block)."
                )
            price_str = results_data["price_str"]
            unit_str = results_data["unit_str"]
            price_elem_html = results_data["price_html"]
            stock_str = results_data["stock_str"] or ""
            card_snippet = results_data["block_html"]
            log.info(
                "Mekrs results block: stock=%r, price=%r, unit=%r",
                stock_str,
                price_str,
                unit_str,
            )

            log.info(f"Price element HTML: {price_elem_html}")
            log.info(f"Raw — price: '{price_str}', unit: '{unit_str}', stock: '{stock_str}'")

            if not price_str:
                log.error(f"Price not found in target card — card HTML snippet:\n{card_snippet}")
                raise RuntimeError(
                    f"Part {supplier_part_no} has no visible price on eshop.mekrs.cz."
                )

            price_raw = _parse_czk_price(price_str)

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
