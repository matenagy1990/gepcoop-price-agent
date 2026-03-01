"""
Playwright scraper for webshop.koelner.hu

Session-aware login flow:
  1. Load saved session cookies from assets/.koelner_session.json (if present)
  2. Navigate to /belepes/ — if Koelner redirects us away, the session is still valid
     → skip login entirely, jump straight to search
  3. If still on /belepes/ (session expired or no session):
     → fill #login_username + #login_password → click #loginbutton
     → verify redirect away from /belepes/
     → save new session to assets/.koelner_session.json for next call

Search flow:
  4. /termekek/?keres={supplier_part_no} — list of product GROUP pages
  5. Iterate each group, look for tr.gy_item.item-selected
  6. Read price from td.NETTO, stock from td.KESZLET .keszlet span
"""

import json
import re
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

log = logging.getLogger("koelner")

LOGIN_URL    = "https://webshop.koelner.hu/belepes/"
SEARCH_URL   = "https://webshop.koelner.hu/termekek/?keres={part_no}"
_SESSION_FILE = Path(__file__).parent.parent / "assets" / ".koelner_session.json"


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_hu_price(price_str: str) -> float:
    """
    Parse Hungarian-formatted price string to float.

    Rules (Hungarian locale):
      dot  = thousands separator  →  "3.740"  = 3740
      comma = decimal separator   →  "7,565"  = 7.565
      both present                →  "1.234,56" = 1234.56
    """
    clean = re.sub(r"[^\d.,]", "", price_str).strip()
    if not clean:
        return 0.0
    if "," in clean and "." in clean:
        clean = clean.replace(".", "").replace(",", ".")
    elif "." in clean:
        clean = clean.replace(".", "")
    else:
        clean = clean.replace(",", ".")
    return float(clean)


async def _save_session(context) -> None:
    """Persist browser cookies/localStorage to disk for reuse on the next call."""
    try:
        state = await context.storage_state()
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        log.info(f"Session saved → {_SESSION_FILE}")
    except Exception as exc:
        log.warning(f"Could not save session: {exc}")


async def _log_login_failure(page, filled_user: str, filled_pass: str,
                              still_on_login: bool, login_form_present: bool) -> None:
    """Dump full diagnostics to the log when a login attempt fails."""
    log.error("═" * 60)
    log.error("KOELNER LOGIN FAILED — diagnostic dump")
    log.error(f"  URL after submit  : {page.url}")
    log.error(f"  still_on_login    : {still_on_login}")
    log.error(f"  login_form_present: {login_form_present}")
    log.error(f"  username used     : '{filled_user}'")
    log.error(f"  password length   : {len(filled_pass)} chars "
              f"({'EMPTY — credential not set' if len(filled_pass) == 0 else 'set'})")

    try:
        log.error(f"  page title        : '{await page.title()}'")
    except Exception:
        pass

    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
        log.error("  --- page body text ---")
        for line in body_text.splitlines():
            line = line.strip()
            if line:
                log.error(f"  | {line}")
    except Exception as exc:
        log.error(f"  (could not read body text: {exc})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = f"/tmp/koelner_login_fail_{ts}.png"
    try:
        await page.screenshot(path=screenshot_path, full_page=True)
        log.error(f"  screenshot saved  : {screenshot_path}")
    except Exception as exc:
        log.error(f"  (screenshot failed: {exc})")

    log.error("═" * 60)


# ── main entry point ──────────────────────────────────────────────────────────

async def fetch_price(supplier_part_no: str, on_progress: Callable | None = None) -> dict:
    async def emit(msg: str):
        log.info(msg)
        if on_progress:
            await on_progress({"step": "browser", "status": "running", "msg": msg})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # Load saved session cookies if available
        context = None
        if _SESSION_FILE.exists():
            try:
                with open(_SESSION_FILE, encoding="utf-8") as f:
                    json.load(f)          # validate JSON before passing to Playwright
                context = await browser.new_context(storage_state=str(_SESSION_FILE))
                log.info(f"Loaded saved session from {_SESSION_FILE}")
            except Exception as exc:
                log.warning(f"Session file unreadable, starting fresh: {exc}")
                _SESSION_FILE.unlink(missing_ok=True)

        if context is None:
            context = await browser.new_context()

        page = await context.new_page()

        async def handle_dialog(dialog):
            log.info(f"Dialog dismissed: '{dialog.message}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            await emit("Opening webshop.koelner.hu…")

            # ── Step 1: check if existing session is still valid ──────────────
            # Koelner does NOT redirect after login — the URL stays on /belepes/.
            # The reliable indicator is whether the login form (#login_username)
            # is present (not logged in) or absent (already logged in).
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            log.info(f"After navigating to LOGIN_URL, landed on: {page.url}")

            # Dismiss cookie/consent banner wherever we are
            try:
                await page.get_by_role("button", name="Rendben").click(timeout=3000)
                await page.wait_for_timeout(500)
                log.info("Cookie notice accepted")
            except PlaywrightTimeout:
                log.info("No cookie notice appeared")

            already_logged_in = await page.locator("#login_username").count() == 0

            if already_logged_in:
                log.info("Session still valid — login form absent, skipping login")
                await emit("Session active — skipping login…")

            else:
                # ── Step 2: full login ────────────────────────────────────────
                log.info("Session expired or absent — performing full login")
                await emit("Logging in to webshop.koelner.hu…")
                log.info(f"Login page URL: {page.url}")

                username = os.getenv("SUPPLIER_C_USERNAME", "")
                log.info(f"Filling login form for user: '{username}'")

                await page.locator("#login_username").fill(username)
                await page.locator("#login_password").fill(os.getenv("SUPPLIER_C_PASSWORD", ""))

                filled_user = await page.locator("#login_username").input_value()
                filled_pass = await page.locator("#login_password").input_value()
                log.info(f"Form filled — username: '{filled_user}', "
                         f"password length: {len(filled_pass)} chars")

                await page.locator("#loginbutton").click()
                log.info("Login button clicked, waiting for redirect…")
                await page.wait_for_timeout(3000)

                # Koelner stays on /belepes/ after both success and failure.
                # Success = login form is gone; failure = form still present.
                login_form_present = await page.locator("#login_username").count() > 0

                if login_form_present:
                    await _log_login_failure(
                        page, filled_user, filled_pass,
                        still_on_login=True, login_form_present=True,
                    )
                    raise RuntimeError(
                        "Login to webshop.koelner.hu failed. Please check credentials."
                    )

                log.info(f"Login successful — login form gone, url={page.url}")
                await _save_session(context)

            # ── Step 3: search ────────────────────────────────────────────────
            search_url = SEARCH_URL.format(part_no=supplier_part_no)
            await emit(f"Searching for part {supplier_part_no} on webshop.koelner.hu…")
            log.info(f"Navigating to search URL: {search_url}")

            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            log.info(f"Search page loaded: {page.url}")

            body_text = await page.locator("body").inner_text(timeout=10000)

            if "Keresés a termékek között (0)" in body_text:
                log.warning(f"Zero results for part {supplier_part_no}")
                raise RuntimeError(f"Part {supplier_part_no} was not found on webshop.koelner.hu.")

            # ── Step 4: collect product-group links ───────────────────────────
            item_links = await page.locator(".item a.products__link").all()
            seen_hrefs: set = set()
            item_hrefs = []
            for link in item_links:
                href = await link.get_attribute("href")
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    item_hrefs.append(href)

            log.info(f"Product groups found (unique): {len(item_hrefs)}")
            if not item_hrefs:
                raise RuntimeError(f"Part {supplier_part_no} was not found on webshop.koelner.hu.")

            # ── Step 5: find the item-selected row ────────────────────────────
            await emit("Reading price and stock from webshop.koelner.hu…")
            target_row = None

            for idx, href in enumerate(item_hrefs):
                modified = re.sub(r"cikkszam=[^&]+", f"cikkszam={supplier_part_no}", href)
                product_url = (
                    f"https://webshop.koelner.hu{modified}"
                    if modified.startswith("/") else modified
                )
                log.info(f"Checking group {idx+1}/{len(item_hrefs)}: {product_url}")
                await page.goto(product_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2500)   # table is lazy-loaded

                selected = await page.locator("table tbody tr.gy_item.item-selected").all()
                if selected:
                    target_row = selected[0]
                    cikkszam_text = ""
                    try:
                        cikkszam_text = await target_row.locator("td.CIKKSZAM").inner_text(timeout=1500)
                    except PlaywrightTimeout:
                        pass
                    log.info(f"  ✓ item-selected found (CIKKSZAM='{cikkszam_text.strip()}') on {page.url}")
                    break

                log.info("  No item-selected row on this group page")

            if target_row is None:
                raise RuntimeError(
                    f"Article '{supplier_part_no}' was not found in any koelner.hu product group."
                )

            # ── Step 6: read price ────────────────────────────────────────────
            price_text = await target_row.locator("td.NETTO").inner_text(timeout=5000)
            price_text = price_text.strip()
            log.info(f"Nettó egységár raw: '{price_text}'")

            if not price_text:
                raise RuntimeError(
                    "Could not read Nettó egységár from koelner.hu variant row. "
                    "Page layout may have changed."
                )

            price_raw      = _parse_hu_price(price_text)
            price_unit_qty = 1      # Nettó egységár is already per-piece
            log.info(f"Parsed price: {price_raw} HUF/db")

            # ── Step 7: read stock ────────────────────────────────────────────
            try:
                stock_text = await target_row.locator(
                    "td.KESZLET .keszlet span"
                ).inner_text(timeout=5000)
                stock_text = stock_text.strip()
                log.info(f"Stock text: '{stock_text}'")
                stock = stock_text if stock_text else "X"
            except PlaywrightTimeout:
                stock = "X"
                log.warning("Stock element not found, assuming out of stock")

            result = {
                "supplier_part_no": supplier_part_no,
                "price_raw":        price_raw,
                "price_unit_qty":   price_unit_qty,
                "currency":         "HUF",
                "unit":             "db",
                "stock":            stock,
                "queried_at":       datetime.now().isoformat(timespec="seconds"),
            }
            log.info(f"Final result: {result}")
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during webshop.koelner.hu scrape: {exc}")
            raise RuntimeError(f"webshop.koelner.hu scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
