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
from agent.runtime_config import get_runtime_env
from browser.session_utils import invalidate_session, load_session, save_session, session_is_fresh
from browser.messages import MSG_NOT_FOUND, MSG_NOT_PRICED, has_numeric_price

load_dotenv()

log = logging.getLogger("kingb2b")

PORTAL_URL = "https://kingb2b.it/PORTAL/"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "kingb2b_session.json"
SESSION_MAX_AGE_HOURS = 20


class _SessionInvalid(RuntimeError):
    """A restored browser session rendered an unusable search response."""


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


async def _get_results_state(page, supplier_part_no: str) -> dict:
    return await page.evaluate(
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


def _is_semantically_empty_search(data: dict) -> bool:
    """The King portal uses this state for stale sessions and missing articles.

    A restored session must be retried with a clean login before this can safely
    be interpreted as a genuine "not found" response.
    """
    return bool(
        data.get("search_text_visible")
        and not data.get("has_family")
        and not data.get("old_row_count")
        and not data.get("article_table_rows")
        and not data.get("no_results_1")
        and not data.get("no_results_2")
        and not data.get("loading_text")
    )


def _unstable_search_error(data: dict, restored_session_unverified: bool) -> RuntimeError:
    if restored_session_unverified:
        return _SessionInvalid(
            "Restored KingB2B session returned an unusable search response."
        )
    if _is_semantically_empty_search(data):
        return RuntimeError(MSG_NOT_FOUND)
    return RuntimeError(
        "Search results did not stabilise on kingb2b.it after clicking the search icon. "
        "The search workflow may have changed."
    )


async def _log_results_state(
    page,
    supplier_part_no: str,
    prefix: str = "KingB2B",
    data: dict | None = None,
) -> dict:
    data = data or await _get_results_state(page, supplier_part_no)
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
    return data


async def _dismiss_promo_popup(page, timeout_ms: int = 8000) -> bool:
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


async def _clear_stale_login_backdrop(page) -> None:
    """Wait for Bootstrap's login backdrop and remove it only when orphaned.

    The portal occasionally leaves ``.modal-backdrop.fade`` behind after a
    successful login. It has no visible modal attached, but intercepts clicks
    on search results. Removing an orphan is safe; an active modal is left
    untouched.
    """
    backdrop = page.locator("div.modal-backdrop")
    if await backdrop.count() == 0:
        return
    try:
        await backdrop.first.wait_for(state="hidden", timeout=3000)
        return
    except PlaywrightTimeout:
        pass

    removed = await page.evaluate(
        """() => {
            const visible = el => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden';
            };
            const activeModal = [...document.querySelectorAll('div.modal')]
                .some(el => visible(el) && (el.classList.contains('in') || el.classList.contains('show')));
            if (activeModal) return 0;
            const backdrops = [...document.querySelectorAll('div.modal-backdrop')];
            backdrops.forEach(el => el.remove());
            document.body.classList.remove('modal-open');
            return backdrops.length;
        }"""
    )
    if removed:
        log.info("KingB2B: removed %s orphaned login modal backdrop(s)", removed)


async def _click_family_result(family_row) -> None:
    """Open a family even when KingB2B's transient progress modal overlaps it."""
    try:
        await family_row.click(timeout=5000)
    except PlaywrightTimeout:
        log.info(
            "KingB2B: family result is covered by a portal overlay — "
            "dispatching its native click handler"
        )
        await family_row.evaluate("el => el.click()")


def _is_king_search_response(response) -> bool:
    """Match the RD3 response generated by the KingB2B search command."""
    request = response.request
    post_data = request.post_data or ""
    return bool(
        request.method == "POST"
        and "WCI=RD3" in response.url
        and "eseguiRicerca" in post_data
    )


async def _submit_search(page, attempt: int) -> None:
    """Submit a search and wait for its own RD3 response, not stale DOM state."""
    try:
        async with page.expect_response(
            _is_king_search_response,
            timeout=10000,
        ) as pending_response:
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
        response = await pending_response.value
        # Reading the body waits for the RD3 payload without leaving
        # Playwright's Response.finished() target-close watcher behind.
        await response.body()
        log.info("KingB2B: search RD3 response received (HTTP %s)", response.status)
    except PlaywrightTimeout:
        # Keep the DOM-based validation as a fallback if the portal changes the
        # request encoding while still rendering a valid result.
        log.warning("KingB2B: search RD3 response was not observed")


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


async def _reset_to_search_state(page) -> None:
    """Navigate back to the portal if the SPA is in an article-table or family state.

    In batch mode the browser stays on the previous part's result view. A fresh
    navigation resets SPA state cleanly so the next search starts from scratch.
    Only fires when article rows or family results are already in the DOM.
    """
    if (
        await page.locator("tr.articoli-row").count() > 0
        or await page.locator("div.singola-famiglia").count() > 0
    ):
        log.info("KingB2B: resetting SPA state — re-navigating to portal")
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_selector("#header-search", timeout=10000)
        await page.wait_for_function(
            "() => document.querySelector('#header-search')?.offsetParent !== null",
            timeout=5000,
        )
        log.info("KingB2B: portal search state restored")


async def _login_and_locate_row(
    page,
    supplier_part_no: str,
    emit: Callable,
    restored_session_unverified: bool = False,
):
    """Log in (if needed), dismiss the promo popup, run the search and return the
    resolved `tr.articoli-row` locator for the part.

    Assumes `page` is already at the portal with #header-search initialised.
    """
    # ── Reset SPA state from any previous search result view ──────────
    await _reset_to_search_state(page)

    # ── Check if login is required ─────────────────────────────
    doc_btn = page.locator("div.button-text-doc")
    is_hidden = await doc_btn.evaluate(
        "el => el.style.display === 'none' || getComputedStyle(el).display === 'none'"
    )

    if is_hidden:
        await emit("Logging in to kingb2b.it…")
        username = get_runtime_env("SUPPLIER_J_USERNAME", "")
        password = get_runtime_env("SUPPLIER_J_PASSWORD", "")
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

        # ── Dismiss the king-inox promo popup that appears right after login ──
        # A full-screen SweetAlert2 modal overlays the whole page and intercepts
        # pointer events on #header-search. Only relevant immediately after login,
        # not for subsequent searches in batch mode (already logged in).
        await _dismiss_promo_popup(page)
        await _clear_stale_login_backdrop(page)
    else:
        log.info("Already logged in")

    # ── Search ────────────────────────────────────────────────
    await emit(f"Searching for {supplier_part_no} on kingb2b.it…")
    search_box = page.locator("#header-search")
    await search_box.click()
    await search_box.fill("")          # JS-level clear — reliable headlessly
    await search_box.type(supplier_part_no, delay=40)  # fires key events SPA expects

    search_started = False
    timeout_state = None
    for attempt in (1, 2):
        await _submit_search(page, attempt)

        try:
            await _wait_for_search_results(page, supplier_part_no)
            search_started = True
            break
        except PlaywrightTimeout:
            log.warning("KingB2B: search attempt %s did not stabilise", attempt)
            timeout_state = await _get_results_state(page, supplier_part_no)
            if _is_semantically_empty_search(timeout_state):
                break

    if not search_started:
        state = timeout_state or await _get_results_state(page, supplier_part_no)
        await _log_results_state(
            page,
            supplier_part_no,
            prefix="KingB2B query-timeout",
            data=state,
        )
        raise _unstable_search_error(state, restored_session_unverified)
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
            if restored_session_unverified:
                raise _SessionInvalid(
                    "Restored KingB2B session returned no article family."
                )
            raise RuntimeError(MSG_NOT_FOUND)

        log.info("Opening KingB2B family result")
        await _click_family_result(family_rows.first)
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
            if restored_session_unverified:
                raise _SessionInvalid(
                    "Restored KingB2B session could not open the article family."
                )
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


async def _open_portal(pw, emit: Callable):
    """Launch a browser, restore the session if fresh, and open the SPA portal
    with #header-search ready. Returns (browser, context, page, restored_session).

    Login itself is handled lazily by `_login_and_locate_row`, which detects the
    logged-in state and only logs in when needed (kingb2b throttles repeated
    automated logins, so we reuse the saved session whenever possible)."""
    session = load_session(SESSION_FILE)
    use_session = bool(session and session_is_fresh(session, SESSION_MAX_AGE_HOURS))

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

    await emit("Opening kingb2b.it…")
    # domcontentloaded + explicit selector waits below replace networkidle.
    # networkidle is unpredictable on SPAs with background polling requests
    # and can block indefinitely; the selector-based waits are more precise.
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_selector("#header-search", timeout=15000)
    await page.wait_for_function(
        "() => document.querySelector('#header-search')?.offsetParent !== null",
        timeout=5000,
    )
    log.info("Portal loaded")
    return browser, ctx, page, use_session


async def _extract_row(page, row_locator, supplier_part_no: str, emit: Callable) -> dict:
    """Wait for the injected price on the located row and parse price + stock."""
    await emit("Reading price and stock from kingb2b.it…")
    # Brief grace period so the SPA's AJAX price-load can dispatch before we
    # start polling. Without this the wait_for_function below occasionally
    # fires on an empty PREZZO cell.
    await page.wait_for_timeout(300)
    for price_attempt in range(2):
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
            break
        except PlaywrightTimeout:
            state = await _get_results_state(page, supplier_part_no)
            await _log_results_state(
                page,
                supplier_part_no,
                prefix="KingB2B price-timeout",
                data=state,
            )
            row_disappeared_to_family = bool(
                price_attempt == 0
                and state.get("has_family")
                and not state.get("old_row_count")
            )
            if not row_disappeared_to_family:
                raise RuntimeError(MSG_NOT_PRICED)

            log.warning(
                "KingB2B: matched article row was replaced by the family view — "
                "opening the family once"
            )
            await emit("KingB2B product view refreshed — reopening the article family…")
            await _click_family_result(page.locator("div.singola-famiglia").first)
            try:
                await page.wait_for_function(
                    """(partNo) => !!document.querySelector(`tr.articoli-row[id="${partNo}"]`)""",
                    arg=supplier_part_no,
                    timeout=12000,
                )
            except PlaywrightTimeout:
                await _log_results_state(
                    page,
                    supplier_part_no,
                    prefix="KingB2B price-recovery-timeout",
                )
                raise RuntimeError(MSG_NOT_PRICED)
            row_locator = page.locator(f'tr.articoli-row[id="{supplier_part_no}"]')

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


async def _search_and_parse(
    page,
    supplier_part_no: str,
    emit: Callable,
    restored_session_unverified: bool = False,
) -> dict:
    """Log in (if needed), locate the part's row and parse it. Reusable per part
    on an already-open portal page."""
    row_locator = await _login_and_locate_row(
        page,
        supplier_part_no,
        emit,
        restored_session_unverified=restored_session_unverified,
    )
    return await _extract_row(page, row_locator, supplier_part_no, emit)


# ── Single lookup (price agent) ──────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        for auth_attempt in range(2):
            browser, ctx, page, restored_session = await _open_portal(pw, emit)
            try:
                return await _search_and_parse(
                    page,
                    supplier_part_no,
                    emit,
                    restored_session_unverified=restored_session,
                )
            except _SessionInvalid:
                if auth_attempt:
                    raise RuntimeError(
                        "Login to kingb2b.it failed. Please check credentials."
                    )
                log.warning("KingB2B restored session is unusable — re-authenticating once")
                invalidate_session(SESSION_FILE)
                await emit("KingB2B session expired — logging in again…")
            except RuntimeError:
                raise
            except Exception as exc:
                log.exception(f"Unexpected error during kingb2b.it scrape: {exc}")
                raise RuntimeError(f"kingb2b.it scrape failed: {exc}") from exc
            finally:
                await browser.close()
                log.info("Browser closed")

    raise RuntimeError("Login to kingb2b.it failed. Please check credentials.")


# ── Batch lookup (batch agent) ───────────────────────────────────────────────

async def fetch_prices(
    part_nos: list[str],
    on_progress: Callable | None = None,
    on_item: Callable | None = None,
) -> list[dict]:
    """Look up several parts in ONE kingb2b session. The portal SPA stays loaded;
    `_login_and_locate_row` logs in only on the first part (it detects the
    logged-in state afterwards) and re-searches for each subsequent part."""
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    results: list[dict] = []
    if not part_nos:
        return results

    total = len(part_nos)

    async with async_playwright() as pw:
        browser, ctx, page, restored_session_unverified = await _open_portal(pw, emit)
        try:
            for i, pn in enumerate(part_nos):
                try:
                    for auth_attempt in range(2):
                        try:
                            r = await _search_and_parse(
                                page,
                                pn,
                                emit,
                                restored_session_unverified=restored_session_unverified,
                            )
                            restored_session_unverified = False
                            break
                        except _SessionInvalid:
                            if auth_attempt:
                                raise RuntimeError(
                                    "Login to kingb2b.it failed. Please check credentials."
                                )
                            log.warning(
                                "KingB2B restored session is unusable during batch — "
                                "re-authenticating once"
                            )
                            invalidate_session(SESSION_FILE)
                            await emit("KingB2B session expired — logging in again…")
                            await browser.close()
                            browser, ctx, page, restored_session_unverified = await _open_portal(
                                pw, emit
                            )
                    results.append(r)
                    if on_item:
                        await on_item(i, total, pn, r, None)
                except RuntimeError as exc:
                    msg = str(exc)
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
                except Exception as exc:
                    log.exception(f"Unexpected error during kingb2b.it scrape ({pn}): {exc}")
                    msg = f"kingb2b.it scrape failed: {exc}"
                    results.append({"supplier_part_no": pn, "error": msg})
                    if on_item:
                        await on_item(i, total, pn, None, msg)
        finally:
            await browser.close()
            log.info("Browser closed")

    return results
