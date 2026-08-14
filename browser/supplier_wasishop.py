"""
Playwright scraper for wasishop.de (Supplier K)

Session flow:
  1. Try to restore saved session from assets/sessions/wasishop_session.json
     - Skip restore if saved_at > 20 h old
  2. Navigate to / — require Wasishop's logout control as authentication proof
     (logged-out pages can still keep the search box and a non-login URL)
  3. If session missing / stale / invalid: full login, save new session
  4. If authentication disappears during a search: invalidate and retry login once

Login flow (fallback):
  1. GET /login_form.php → dismiss cookie → fill Name + Passwort → Anmelden
  2. Redirects to /de/handel/index.php on success

Search flow:
  5. Fill input[name='search'] → press Enter → lands on Artikelliste.php
  6. Wait for the exact article card's price (prices are JS-injected)
  7. Parse price and stock only inside the exact supplier article card

Price structure — two cases:
  a) Tiered ("Staffelpreis"): art_popup_infobox with "Mindestmenge" header contains
     rows of (min qty, price/100) e.g. 0 Stk. → 30,17€ / 1.000 Stk. → 27,15€ / 52.000 Stk. → 27,15€
     → use the MIDDLE tier price (index len//2)
  b) Single price: div.price.discount → e.g. "0,79 €"
  Both prices are always per 100 pieces ("Preis / 100" column).

Stock:
  Extracted from the span sequence:  orderNumber → partNo → STOCK_VALUE
  Format: German thousands separator ("37.000" = 37000, "492.600" = 492600)

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
from agent.runtime_config import get_runtime_env
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("wasishop")

LOGIN_URL = "https://www.wasishop.de/login_form.php"
HOME_URL  = "https://www.wasishop.de"

_SESSION_FILE      = Path(__file__).parent.parent / "assets" / "sessions" / "wasishop_session.json"
_SESSION_MAX_AGE_H = 20


class _SessionExpired(RuntimeError):
    """The page is reachable, but Wasishop no longer considers us logged in."""


async def _is_authenticated(page) -> bool:
    """Use Wasishop's account controls, not the URL/search box, as auth proof.

    Logged-out Wasishop pages can remain under ``/de/`` and still expose the
    catalogue search input.  A valid buyer session exposes a logout link.
    """
    if "login_form" in page.url:
        return False
    return await page.locator(
        "a[href*='logout'], a[href*='abmelden'], a:has-text('Abmelden')"
    ).count() > 0


def _parse_eur(text: str) -> float:
    """Parse a German/EUR price string: '27,15 €' or '0,79\xa0€' → 27.15"""
    clean = re.sub(r"[€\s\u00a0]", "", text).strip()
    if "," in clean and "." in clean:
        clean = clean.replace(".", "").replace(",", ".")
    elif "," in clean:
        clean = clean.replace(",", ".")
    return float(clean)


def _parse_stock(text: str) -> int:
    """Parse German stock number: '37.000' → 37000, '492.600' → 492600"""
    m = re.search(r"[\d.]+", text)
    if not m:
        return 0
    return int(m.group().replace(".", ""))


# ── Session / login (shared) ─────────────────────────────────────────────────

async def _login_or_restore(pw, emit: Callable):
    """Launch a browser and return (browser, context, page) on a logged-in
    wasishop.de context. Shared by both entrypoints."""
    session = load_session(_SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, _SESSION_MAX_AGE_H))

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
    await emit("Opening wasishop.de…")

    # ── Step 1: try session restore ───────────────────────────────────
    if use_session:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_function(
                """() => location.href.includes('login_form')
                    || !!document.querySelector("a[href*='login']")
                    || !!document.querySelector("a[href*='logout'], a[href*='abmelden']")""",
                timeout=8000,
            )
        except PlaywrightTimeout:
            log.warning("No authenticated/login marker appeared after session restore")
        log.info(f"After session restore, URL: {page.url}")

        if not await _is_authenticated(page):
            log.warning("Session invalid — falling back to full login")
            use_session = False
            invalidate_session(_SESSION_FILE)
            await browser.close()
            browser  = await pw.chromium.launch(headless=True)
            context  = await browser.new_context()
            page     = await context.new_page()
        else:
            log.info("Session valid — login skipped")

    # ── Step 2: full login (if needed) ───────────────────────────────
    if not use_session:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("input[name='name'], input[name='password']", timeout=8000)
        log.info(f"Login page: {page.url}")

        try:
            await page.locator("button[aria-label='dismiss cookie message']").click(timeout=4000)
            log.info("Cookie banner dismissed")
        except PlaywrightTimeout:
            log.info("No cookie banner")

        await emit("Logging in to wasishop.de…")
        username = get_runtime_env("SUPPLIER_K_USERNAME", "")
        password = get_runtime_env("SUPPLIER_K_PASSWORD", "")
        log.info(f"Logging in as: {username}")

        await page.get_by_role("textbox", name="Name").fill(username)
        await page.get_by_role("textbox", name="Passwort").fill(password)
        await page.get_by_role("button", name="Anmelden").click()
        try:
            await page.wait_for_function(
                """() => !location.href.includes('login_form')
                    && !!document.querySelector("input[name='search']")
                    && !!document.querySelector("a[href*='logout'], a[href*='abmelden']")""",
                timeout=12000,
            )
        except PlaywrightTimeout:
            pass

        if not await _is_authenticated(page):
            raise RuntimeError("Login to wasishop.de failed. Please check credentials.")
        log.info(f"Login successful: {page.url}")

        await save_session(context, _SESSION_FILE)

    return browser, context, page


async def _search_and_parse(page, supplier_part_no: str, emit: Callable) -> dict:
    """Search one part on a logged-in wasishop page and parse price + stock."""
    # ── Step 3: search ────────────────────────────────────────────────
    await emit(f"Searching for {supplier_part_no} on wasishop.de…")
    search = page.locator("input[name='search']")
    await search.fill(supplier_part_no)
    await search.press("Enter")
    await page.wait_for_load_state("networkidle")
    try:
        await page.wait_for_function(
            """partNo => {
                const cards = Array.from(document.querySelectorAll('.shipping_card_pos'));
                const card = cards.find(el => Array.from(el.querySelectorAll('span'))
                    .some(span => span.innerText.trim() === partNo));
                const text = document.body.innerText;
                const loggedOut = !!document.querySelector("a[href*='login']")
                    && !document.querySelector("a[href*='logout'], a[href*='abmelden']");
                return loggedOut
                    || !!card?.querySelector('div.price.discount')
                    || text.includes('keine Artikel')
                    || text.includes('momentan keine Artikel');
            }""",
            arg=supplier_part_no,
            timeout=10000,
        )
    except PlaywrightTimeout:
        log.warning("Price / empty-state text did not appear after 10s — attempting extraction anyway")
    log.info(f"Search results: {page.url}")

    if not await _is_authenticated(page):
        raise _SessionExpired("Wasishop session expired during search")

    body_text = await page.locator("body").inner_text()
    if "keine Artikel" in body_text or "momentan keine Artikel" in body_text:
        raise RuntimeError(MSG_NOT_FOUND)

    await emit("Reading price and stock from wasishop.de…")

    # ── Extract price ──────────────────────────────────────────────
    raw_data = await page.evaluate(f"""() => {{
        const partNo = {repr(supplier_part_no)};

        // A search can return several similar variants. Only read the card
        // whose displayed Wasishop article number exactly matches partNo.
        const cards = Array.from(document.querySelectorAll('.shipping_card_pos'));
        const card = cards.find(el => Array.from(el.querySelectorAll('span'))
            .some(span => span.innerText.trim() === partNo));
        if (!card) return {{ found: false, tiers: [], singles: [], stock: '' }};

        // Case A: tiered pricing — art_popup_infobox with 'Mindestmenge'
        const tiers = [];
        for (const box of card.querySelectorAll('.art_popup_infobox')) {{
            if (box.innerText.includes('Mindestmenge')) {{
                const infos = Array.from(box.querySelectorAll('.art_popup_info'))
                    .map(el => el.innerText.trim())
                    .filter(t => t && t !== 'Mindestmenge' && t !== 'Preis');
                // pairs: qty at [0,2,4,...], price at [1,3,5,...]
                for (let i = 1; i < infos.length; i += 2) tiers.push(infos[i]);
                break;
            }}
        }}

        // Case B: single price — div.price.discount
        const singleEls = card.querySelectorAll('div.price.discount');
        const singles = [...new Set(Array.from(singleEls).map(el => el.innerText.trim()))];

        // Stock — span immediately after the span containing partNo
        // (pattern in DOM: ... 'orderNumber' span → partNo span → STOCK span ...)
        let stock = '';
        const spans = Array.from(card.querySelectorAll('span'));
        for (let i = 0; i < spans.length - 1; i++) {{
            if (spans[i].innerText.trim() === partNo) {{
                // next non-empty span
                for (let j = i + 1; j < spans.length; j++) {{
                    const t = spans[j].innerText.trim();
                    if (t && t !== partNo) {{ stock = t; break; }}
                }}
                break;
            }}
        }}

        return {{ found: true, tiers, singles, stock }};
    }}""")

    log.info(f"Raw data: {raw_data}")

    tiers      = raw_data.get("tiers", [])
    singles    = raw_data.get("singles", [])
    stock_text = raw_data.get("stock", "")

    if not raw_data.get("found"):
        raise RuntimeError(MSG_NOT_FOUND)

    if tiers:
        middle    = tiers[len(tiers) // 2]
        price_raw = _parse_eur(middle)
        log.info(f"Tiered prices: {tiers} → using middle: {middle!r} → {price_raw}")
    elif singles:
        price_raw = _parse_eur(singles[0])
        log.info(f"Single price: {singles[0]!r} → {price_raw}")
    else:
        # The product is listed, but neither a tiered nor a single price is shown.
        raise RuntimeError(MSG_NOT_PRICED)

    price_unit_qty = 100  # always per 100 pcs

    stock_value = _parse_stock(stock_text)
    log.info(f"Stock text: {stock_text!r} → {stock_value}")
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


# ── Single lookup (price agent) ──────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        for auth_attempt in range(2):
            browser, context, page = await _login_or_restore(pw, emit)
            try:
                return await _search_and_parse(page, supplier_part_no, emit)
            except _SessionExpired:
                if auth_attempt:
                    raise RuntimeError("Login to wasishop.de failed. Please check credentials.")
                log.warning("Session expired during search — re-authenticating once")
                invalidate_session(_SESSION_FILE)
                await emit("Wasishop session expired — logging in again…")
            except RuntimeError:
                raise
            except Exception as exc:
                log.exception(f"Unexpected error during wasishop.de scrape: {exc}")
                raise RuntimeError(f"wasishop.de scrape failed: {exc}") from exc
            finally:
                await browser.close()
                log.info("Browser closed")

    raise RuntimeError("Login to wasishop.de failed. Please check credentials.")


# ── Batch lookup (batch agent) ───────────────────────────────────────────────

async def fetch_prices(
    part_nos: list[str],
    on_progress: Callable | None = None,
    on_item: Callable | None = None,
) -> list[dict]:
    """Look up several parts in ONE wasishop session (login once, reuse browser)."""
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    results: list[dict] = []
    if not part_nos:
        return results

    total = len(part_nos)

    async with async_playwright() as pw:
        browser, context, page = await _login_or_restore(pw, emit)
        try:
            for i, pn in enumerate(part_nos):
                try:
                    for auth_attempt in range(2):
                        try:
                            r = await _search_and_parse(page, pn, emit)
                            break
                        except _SessionExpired:
                            if auth_attempt:
                                raise RuntimeError("Login to wasishop.de failed. Please check credentials.")
                            log.warning("Session expired during batch search — re-authenticating once")
                            invalidate_session(_SESSION_FILE)
                            await emit("Wasishop session expired — logging in again…")
                            await browser.close()
                            browser, context, page = await _login_or_restore(pw, emit)
                    results.append(r)
                    if on_item:
                        await on_item(i, total, pn, r, None)
                except RuntimeError as exc:
                    msg = str(exc)
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
                except Exception as exc:
                    log.exception(f"Unexpected error during wasishop.de scrape ({pn}): {exc}")
                    msg = f"wasishop.de scrape failed: {exc}"
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
        finally:
            await browser.close()
            log.info("Browser closed")

    return results
