"""
Playwright scraper for wasishop.de (Supplier K)

Login flow:
  1. GET /login_form.php → dismiss cookie → fill Name + Passwort → Anmelden
  2. Redirects to /de/handel/index.php on success

Search flow:
  3. Fill input[name='search'] → press Enter → lands on Artikelliste.php
  4. Wait for networkidle (prices are JS-injected after page load)

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

log = logging.getLogger("wasishop")

LOGIN_URL    = "https://www.wasishop.de/login_form.php"
HOME_URL     = "https://www.wasishop.de/de/handel/index.php"
SESSION_FILE = Path(__file__).parent.parent / "assets" / "sessions" / "wasishop_session.json"


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
    return "login_form" not in page.url


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
            await page.wait_for_timeout(1500)
            try:
                await page.locator("button[aria-label='dismiss cookie message']").click(timeout=4000)
                await page.wait_for_timeout(500)
            except PlaywrightTimeout:
                pass
            username = os.getenv("SUPPLIER_K_USERNAME", "")
            password = os.getenv("SUPPLIER_K_PASSWORD", "")
            log.info(f"Logging in as: {username}")
            await page.get_by_role("textbox", name="Name").fill(username)
            await page.get_by_role("textbox", name="Passwort").fill(password)
            await page.get_by_role("button", name="Anmelden").click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)
            if "login_form" in page.url:
                raise RuntimeError("Login to wasishop.de failed. Please check credentials.")
            log.info(f"Login successful: {page.url}")

        try:
            await emit("Opening wasishop.de…")

            # --- 1. Restore saved session or do fresh login ---
            saved_cookies = _load_saved_cookies()
            if saved_cookies:
                log.info("Restoring saved session cookies")
                await ctx.add_cookies(saved_cookies)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                if not await _is_logged_in(page):
                    log.warning("Saved session expired — performing fresh login")
                    SESSION_FILE.unlink(missing_ok=True)
                    await emit("Logging in to wasishop.de…")
                    await _do_login()
                    _save_cookies(await ctx.cookies())
                else:
                    log.info("Session restored successfully")
            else:
                await emit("Logging in to wasishop.de…")
                await _do_login()
                _save_cookies(await ctx.cookies())

            # Search
            await emit(f"Searching for {supplier_part_no} on wasishop.de…")
            search = page.locator("input[name='search']")
            await search.fill(supplier_part_no)
            await search.press("Enter")
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(3000)   # prices are injected after page load
            log.info(f"Search results: {page.url}")

            body_text = await page.locator("body").inner_text()
            if "keine Artikel" in body_text or "momentan keine Artikel" in body_text:
                raise RuntimeError(f"Part {supplier_part_no} was not found on wasishop.de.")

            await emit("Reading price and stock from wasishop.de…")

            # ── Extract price ──────────────────────────────────────────────
            raw_data = await page.evaluate(f"""() => {{
                const partNo = {repr(supplier_part_no)};

                // Case A: tiered pricing — art_popup_infobox with 'Mindestmenge'
                const tiers = [];
                for (const box of document.querySelectorAll('.art_popup_infobox')) {{
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
                const singleEls = document.querySelectorAll('div.price.discount');
                const singles = [...new Set(Array.from(singleEls).map(el => el.innerText.trim()))];

                // Stock — span immediately after the span containing partNo
                // (pattern in DOM: ... 'orderNumber' span → partNo span → STOCK span ...)
                let stock = '';
                const spans = Array.from(document.querySelectorAll('span'));
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

                return {{ tiers, singles, stock }};
            }}""")

            log.info(f"Raw data: {raw_data}")

            tiers   = raw_data.get("tiers", [])
            singles = raw_data.get("singles", [])
            stock_text = raw_data.get("stock", "")

            if tiers:
                # Take the middle tier (index len//2)
                middle = tiers[len(tiers) // 2]
                price_raw = _parse_eur(middle)
                log.info(f"Tiered prices: {tiers} → using middle: {middle!r} → {price_raw}")
            elif singles:
                price_raw = _parse_eur(singles[0])
                log.info(f"Single price: {singles[0]!r} → {price_raw}")
            else:
                raise RuntimeError(
                    "Could not read price from wasishop.de. Page layout may have changed."
                )

            # Price is always per 100 pcs ("Preis / 100" column)
            price_unit_qty = 100

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

        except RuntimeError:
            raise
        except Exception as exc:
            log.exception(f"Unexpected error during wasishop.de scrape: {exc}")
            raise RuntimeError(f"wasishop.de scrape failed: {exc}") from exc
        finally:
            await browser.close()
            log.info("Browser closed")
