"""
Playwright scraper for kingb2b.it (Supplier J)

Login flow:
  1. GET /PORTAL/ → SPA loads (wait for #header-search)
  2. Check login state: div.button-text-doc style="display:none" → not logged in
  3. Click div.header-button.account → login modal opens
  4. Fill Username + Password → click LOGIN button
  5. Confirmed when DOCUMENTI / TRACKING buttons become visible

Search flow:
  6. Fill #header-search with part number → click the search icon
  7. Wait for "LA TUA RICERCA: ..." to appear and family results to load
  8. Click the matching family result
  9. Wait for tr.articoli-row elements to appear
  10. Resolve row by id="PART_NO", fall back to an exact-text row match

Price structure:
  - td[data-cell="PREZZO"] contains e.g. "0,60 %" or "7,68 %"
  - "%" → price is per 100 units (price_unit_qty = 100)
  - "N" → price is per 1 unit (price_unit_qty = 1)
  - Italian decimal: comma → dot

Stock structure:
  - td[data-cell="STOCK"] contains divs:
    - div.dispo-ok   → current stock (e.g. "26.000" = 26,000 units)
    - div.dispo-incoming → incoming stock + date (e.g. "492.000 13/05/26")
    - div.dispo-ko   → out of stock message
  - Parse first number from whichever div has content
  - Italian thousands separator: dot → strip

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
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("kingb2b")

PORTAL_URL = "https://kingb2b.it/PORTAL/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "kingb2b_session.json"
SESSION_MAX_AGE_HOURS = 20


def _parse_eur(text: str) -> float:
    """Parse Italian price string: '0,60' or '7,68' → float"""
    clean = text.strip().replace(".", "").replace(",", ".")
    return float(re.search(r"[\d.]+", clean).group())


def _parse_stock(text: str) -> int:
    """Parse Italian stock: '492.000 13/05/26' → 492000, '26.000' → 26000"""
    m = re.search(r"[\d.]+", text)
    if not m:
        return 0
    return int(m.group().replace(".", ""))


async def _log_results_state(page, supplier_part_no: str, prefix: str = "KingB2B") -> None:
    data = await page.evaluate(
        """(partNo) => {
            const txt = document.body.innerText || '';
            return {
                url: location.href,
                search_text_visible: txt.includes(`LA TUA RICERCA: "${partNo}"`) || txt.includes(`La tua ricerca: "${partNo}"`),
                has_family: !!document.querySelector('div.singola-famiglia'),
                family_count: document.querySelectorAll('div.singola-famiglia').length,
                old_row_count: document.querySelectorAll('tr.articoli-row').length,
                article_table_rows: document.querySelectorAll('table.tabella-articoli tr').length,
                article_headers_visible: txt.includes('PREZZO') && txt.includes('STOCK') && txt.includes('DESCRIZIONE'),
                loading_text: txt.includes('Attendere prego'),
                no_results_1: txt.toLowerCase().includes('nessun risultato'),
                no_results_2: txt.toLowerCase().includes('nessun articolo'),
                body_snippet: txt.slice(650, 2600),
            };
        }""",
        supplier_part_no,
    )
    log.info(
        "%s state: url=%s, search_text=%r, has_family=%r(%s), old_rows=%s, article_rows=%s, "
        "article_headers=%r, loading=%r, no_results=%r/%r",
        prefix,
        data["url"],
        data["search_text_visible"],
        data["has_family"],
        data["family_count"],
        data["old_row_count"],
        data["article_table_rows"],
        data["article_headers_visible"],
        data["loading_text"],
        data["no_results_1"],
        data["no_results_2"],
    )
    log.info("%s body snippet: %s", prefix, data["body_snippet"])


async def _dismiss_promo_popup(page, timeout_ms: int = 4000) -> bool:
    """Best-effort close of the king-inox promo modal shown right after login.

    After a successful login the portal opens a full-screen SweetAlert2 modal
    (div.swal2-container holding an lp.king-inox.com iframe). It overlays the
    whole page and intercepts pointer events, so any click on #header-search
    times out. This helper is intentionally non-fatal: if no popup appears, or
    if dismissing fails, the normal search flow continues unaffected.

    Returns True only when a popup was detected and dismissed.
    """
    container = page.locator("div.swal2-container")
    try:
        await container.first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeout:
        return False  # no popup this time — nothing to do
    except Exception as exc:
        log.warning(f"KingB2B: promo popup probe failed (ignored): {exc}")
        return False

    log.info("KingB2B: promo popup detected — dismissing")
    try:
        # Clean programmatic close first (no pointer events involved); fall back
        # to clicking the visible CLOSE button if the global Swal API is absent.
        await page.evaluate(
            """() => {
                try {
                    if (typeof Swal !== 'undefined' && typeof Swal.close === 'function') {
                        Swal.close();
                    }
                } catch (e) {}
                const btn = document.querySelector('div.swal2-container .swal2-confirm');
                if (btn) btn.click();
            }"""
        )
        await container.first.wait_for(state="hidden", timeout=5000)
        log.info("KingB2B: promo popup dismissed")
        return True
    except PlaywrightTimeout:
        log.warning("KingB2B: promo popup still present after dismiss attempt")
        return False
    except Exception as exc:
        log.warning(f"KingB2B: promo popup dismiss error (ignored): {exc}")
        return False


async def _wait_for_search_results(page, supplier_part_no: str) -> None:
    await page.wait_for_function(
        """(partNo) => {
            const txt = (document.body.innerText || '').toLowerCase();
            const hasQuery = txt.includes(`la tua ricerca: "${partNo.toLowerCase()}"`);
            const hasFamily = !!document.querySelector('div.singola-famiglia');
            const hasExactRow = !!document.querySelector(`tr.articoli-row[id="${partNo}"]`);
            const noResults = txt.includes('nessun risultato') || txt.includes('nessun articolo');
            return hasQuery && (hasFamily || hasExactRow || noResults);
        }""",
        arg=supplier_part_no,
        timeout=15000,
    )


async def _login_and_locate_row(page, supplier_part_no: str, emit: Callable):
    """Log in (if needed), dismiss the promo popup, run the search and return the
    resolved `tr.articoli-row` locator for the part.

    Assumes `page` is already at the portal with #header-search initialised.
    """
    # ── Check if login is required ─────────────────────────────
    doc_btn = page.locator("div.button-text-doc")
    is_hidden = await doc_btn.evaluate(
        "el => el.style.display === 'none' || getComputedStyle(el).display === 'none'"
    )

    if is_hidden:
        await emit("Logging in to kingb2b.it…")
        username = os.getenv("SUPPLIER_J_USERNAME", "")
        password = os.getenv("SUPPLIER_J_PASSWORD", "")
        log.info(f"Logging in as: {username}")

        # Open login modal
        await page.locator("div.header-button.account").first.click()
        await page.wait_for_selector("input[placeholder='Username']", timeout=6000)

        await page.get_by_role("textbox", name="Username").fill(username)
        await page.get_by_role("textbox", name="Password").fill(password)
        await page.get_by_role("button", name="LOGIN").click()

        # Wait for DOCUMENTI to become visible (login confirmed)
        try:
            await page.wait_for_function(
                "() => getComputedStyle(document.querySelector('div.button-text-doc')).display !== 'none'",
                timeout=10000,
            )
        except PlaywrightTimeout:
            raise RuntimeError("Login to kingb2b.it failed. Please check credentials.")

        log.info("Login successful")
        try:
            await save_session(page.context, SESSION_FILE)
        except Exception as exc:
            log.warning(f"Could not persist session: {exc}")
    else:
        log.info("Already logged in")

    # ── Dismiss the king-inox promo popup that overlays the portal ──
    # A full-screen SweetAlert2 modal appears after login and would
    # otherwise intercept the click on #header-search below.
    await _dismiss_promo_popup(page)

    # ── Search ────────────────────────────────────────────────
    await emit(f"Searching for {supplier_part_no} on kingb2b.it…")
    search_box = page.locator("#header-search")
    await search_box.click()
    await search_box.press("Control+A")
    await search_box.press("Backspace")
    await search_box.type(supplier_part_no, delay=40)

    search_started = False
    for attempt in (1, 2):
        if attempt == 1:
            await page.locator("div.bottone-esegui-ricerca").click()
        else:
            log.info("KingB2B: retrying search submission via bottoneEseguiRicerca()")
            await page.evaluate(
                """() => {
                    if (typeof bottoneEseguiRicerca === 'function') {
                        bottoneEseguiRicerca();
                    }
                }"""
            )

        try:
            await _wait_for_search_results(page, supplier_part_no)
            search_started = True
            break
        except PlaywrightTimeout:
            log.warning("KingB2B: search attempt %s did not stabilise", attempt)

    if not search_started:
        await _log_results_state(page, supplier_part_no, prefix="KingB2B query-timeout")
        raise RuntimeError(
            "Search results did not stabilise on kingb2b.it after clicking the search icon. "
            "The search workflow may have changed."
        )
    log.info("Search query confirmed")

    # Check for "not found"
    body_text = await page.locator("body").inner_text()
    if "nessun risultato" in body_text.lower() or "nessun articolo" in body_text.lower():
        raise RuntimeError(MSG_NOT_FOUND)

    article_row = page.locator(f'tr.articoli-row[id="{supplier_part_no}"]')

    if await article_row.count() == 0:
        family_rows = page.locator("div.singola-famiglia")
        family_count = await family_rows.count()
        if family_count == 0:
            await _log_results_state(page, supplier_part_no, prefix="KingB2B no-family-no-row")
            raise RuntimeError(MSG_NOT_FOUND)

        log.info("Opening KingB2B family result")
        await family_rows.first.click()
        try:
            await page.wait_for_function(
                """(partNo) => {
                    const row = document.querySelector(`tr.articoli-row[id="${partNo}"]`);
                    if (row) return true;
                    return [...document.querySelectorAll('tr.articoli-row')]
                        .some(r => (r.innerText || '').includes(partNo));
                }""",
                arg=supplier_part_no,
                timeout=12000,
            )
        except PlaywrightTimeout:
            await _log_results_state(page, supplier_part_no, prefix="KingB2B family-expand-timeout")
            raise RuntimeError(MSG_NOT_FOUND)

    # Resolve row locator
    article_row = page.locator(f'tr.articoli-row[id="{supplier_part_no}"]')
    if await article_row.count() > 0:
        log.info("KingB2B: matched article row by ID")
        return article_row

    exact_text_row = page.locator("tr.articoli-row").filter(has_text=supplier_part_no).first
    if await exact_text_row.count() > 0:
        log.info("KingB2B: matched article row by text")
        return exact_text_row

    await _log_results_state(page, supplier_part_no, prefix="KingB2B no-row-found")
    raise RuntimeError(MSG_NOT_FOUND)


async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    session = load_session(SESSION_FILE)
    # KingB2B keeps enough UI/search state in the portal that restored sessions
    # can leave the search flow inconsistent. A fresh login is slower, but much
    # more reliable than reusing a cached session here.
    use_session = False

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
            await emit("Opening kingb2b.it…")
            # networkidle is required when restoring a session so the SPA fully
            # initialises its search API state before we type a query.
            wait_until = "networkidle" if use_session else "domcontentloaded"
            await page.goto(PORTAL_URL, wait_until=wait_until, timeout=30000)
            # Wait for SPA to fully initialise
            await page.wait_for_selector("#header-search", timeout=15000)
            await page.wait_for_function(
                "() => document.querySelector('#header-search')?.offsetParent !== null",
                timeout=5000,
            )
            log.info("Portal loaded")

            row_locator = await _login_and_locate_row(page, supplier_part_no, emit)

            # ── Wait for price to be injected (requires login) ─────────
            await emit("Reading price and stock from kingb2b.it…")
            try:
                await page.wait_for_function(
                    f"""() => {{
                        const row = document.querySelector('tr.articoli-row[id="{supplier_part_no}"]');
                        if (row) return row.querySelector('td[data-cell="PREZZO"]')?.innerText.trim() !== '';
                        const rows = [...document.querySelectorAll('table.tabella-articoli tr')];
                        const generic = rows.find(r => (r.innerText || '').includes("{supplier_part_no}"));
                        return !!generic && /\\d+[,.]?\\d*\\s*(%|N)/.test((generic.innerText || '').trim());
                    }}""",
                    timeout=8000,
                )
            except PlaywrightTimeout:
                await _log_results_state(page, supplier_part_no, prefix="KingB2B price-timeout")
                raise RuntimeError(MSG_NOT_PRICED)

            # ── Extract price ──────────────────────────────────────────
            if await row_locator.locator('td[data-cell="PREZZO"]').count():
                prezzo_text = await row_locator.locator('td[data-cell="PREZZO"]').inner_text()
                prezzo_text = prezzo_text.strip()
            else:
                row_text = (await row_locator.inner_text()).strip()
                m = re.search(r'(\d+[,.]\d+)\s*(%|N)\b', row_text)
                if not m:
                    raise RuntimeError(MSG_NOT_PRICED)
                prezzo_text = f"{m.group(1)} {m.group(2)}"
            log.info(f"PREZZO cell: {prezzo_text!r}")

            # Parse price value (Italian decimal comma)
            price_raw = _parse_eur(prezzo_text)

            # Determine unit: "%" → per 100 pcs, "N" → per 1 pc
            if "%" in prezzo_text:
                price_unit_qty = 100
            elif "N" in prezzo_text:
                price_unit_qty = 1
            else:
                # Fallback: use BOX column quantity
                box_locator = row_locator.locator('td[data-cell="BOX"]')
                box_text = await box_locator.inner_text() if await box_locator.count() else ""
                box_text = box_text.strip().replace(".", "")
                try:
                    price_unit_qty = int(box_text)
                except ValueError:
                    price_unit_qty = 1
            log.info(f"Price: {price_raw} EUR / {price_unit_qty} pcs")

            # ── Extract stock ──────────────────────────────────────────
            stock_value = 0
            stock_cell = row_locator.locator('td[data-cell="STOCK"]')

            if await stock_cell.count():
                for cls in ["dispo-ok", "dispo-incoming"]:
                    locator = stock_cell.locator(f".{cls}")
                    if await locator.count():
                        div_text = (await locator.inner_text()).strip()
                        if div_text:
                            stock_value = _parse_stock(div_text)
                            log.info(f"Stock from .{cls}: {div_text!r} → {stock_value}")
                            break
            else:
                row_text = (await row_locator.inner_text()).strip()
                stock_match = re.search(r'(\d[\d.]*)\s+(?:STOCK|NOTA|VS CODICE|$)', row_text)
                if stock_match:
                    stock_value = _parse_stock(stock_match.group(1))

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
            log.exception(f"Unexpected error during kingb2b.it scrape: {exc}")
            raise RuntimeError(f"kingb2b.it scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
