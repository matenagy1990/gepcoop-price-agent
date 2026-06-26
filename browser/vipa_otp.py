"""Shared Vipa OTP login engine.

Headless Playwright flow that holds a browser session open while the buyer
fetches the one-time token from the Vipa login email, then submits it to
finalise and persist the session. Extracted so both the GepcoopPriceAgent and
the Batch Price Agent can drive the exact same proven flow.

Public surface:
  vipa_login_state          — shared mutable state dict (single source of truth)
  start_vipa_otp_flow()     — kick off the headless OTP login (idempotent)
  vipa_session_is_live()    — verify the saved session is fresh AND accepted
"""

import asyncio
import logging
import os
import re

from agent.runtime_config import get_runtime_env

log = logging.getLogger("vipa_otp")

# Shared mutable state for the running OTP flow. Mutated in place so importers
# all observe the same object.
vipa_login_state: dict = {
    "active":      False,
    "token_event": None,   # asyncio.Event — set when buyer submits the token
    "ready_event": None,   # asyncio.Event — set when the OTP token field is visible
    "done_event":  None,   # asyncio.Event — set when task finishes
    "token":       None,   # the submitted OTP string
    "error":       None,   # error message from the task, or None on success
    "task":        None,   # the running asyncio.Task
    "stage":       None,   # short diagnostic stage for logs/timeouts
    "last_url":    None,
}


def _vipa_set_stage(stage: str, page=None) -> None:
    vipa_login_state["stage"] = stage
    if page is not None:
        try:
            vipa_login_state["last_url"] = page.url
        except Exception:
            pass
    log.info(
        "Vipa OTP stage: %s%s",
        stage,
        f" url={vipa_login_state.get('last_url')}" if vipa_login_state.get("last_url") else "",
    )


async def _run_vipa_otp_login() -> None:
    """Background task: holds a headless Playwright session open for Vipa OTP login."""
    from playwright.async_api import async_playwright as _pw
    from playwright.async_api import TimeoutError as _PwTimeout
    from browser.session_utils import save_session as _save_session
    from browser.supplier_vipa import (
        SESSION_FILE as _VIPA_SESSION,
        LOGIN_URL as _VIPA_LOGIN,
        HOME_URL as _VIPA_HOME,
        _is_logged_in as _vipa_is_logged_in,
    )

    async def _fill_first_input(page, locators: list, value: str, timeout: int = 15000) -> None:
        last_exc = None
        deadline = asyncio.get_running_loop().time() + (timeout / 1000)
        while asyncio.get_running_loop().time() < deadline:
            for locator in locators:
                try:
                    if await locator.first.count() == 0:
                        continue
                    if not await locator.first.is_visible(timeout=500):
                        continue
                    await locator.first.fill(value, timeout=1500)
                    return
                except Exception as exc:
                    last_exc = exc
            await asyncio.sleep(0.25)
        raise last_exc or RuntimeError("Vipa input mező nem található.")

    async def _click_first_button(page, locators: list, timeout: int = 15000) -> None:
        last_exc = None
        deadline = asyncio.get_running_loop().time() + (timeout / 1000)
        while asyncio.get_running_loop().time() < deadline:
            for locator in locators:
                try:
                    if await locator.first.count() == 0:
                        continue
                    if not await locator.first.is_visible(timeout=500):
                        continue
                    await locator.first.click(timeout=1500)
                    return
                except Exception as exc:
                    last_exc = exc
            await asyncio.sleep(0.25)
        raise last_exc or RuntimeError("Vipa bejelentkezés gomb nem található.")

    try:
        _vipa_set_stage("starting")
        email = get_runtime_env("SUPPLIER_VIPA_USERNAME", "")
        if not email:
            vipa_login_state["error"] = "SUPPLIER_VIPA_USERNAME nincs beállítva a .env fájlban."
            return

        async with _pw() as pw:
            _vipa_set_stage("launching-browser")
            browser = await pw.chromium.launch(headless=True)
            _vipa_set_stage("creating-context")
            context = await browser.new_context()
            page = await context.new_page()
            page.on("requestfailed", lambda req: log.warning("Vipa OTP request failed: %s %s", req.url, req.failure))
            try:
                _vipa_set_stage("opening-login-page", page)
                await page.goto(_VIPA_LOGIN, wait_until="domcontentloaded", timeout=30000)
                _vipa_set_stage("login-page-loaded", page)
                login_form = page.locator('form[action*="/login"]').last
                await login_form.wait_for(state="visible", timeout=15000)
                await _fill_first_input(page, [
                    login_form.locator('input[type="email"]'),
                    login_form.locator('input[name*="email" i]'),
                    login_form.locator('input[placeholder*="email" i]'),
                ], email)
                _vipa_set_stage("email-filled", page)
                await _click_first_button(page, [
                    login_form.locator('input[type="submit"]'),
                    login_form.locator('button[type="submit"]'),
                    login_form.get_by_role("button", name=re.compile(r"log\s*in|login|sign\s*in", re.I)),
                ])
                _vipa_set_stage("login-clicked-waiting-token", page)

                # Wait for Token input (OTP email sent at this point)
                try:
                    await _fill_first_input(page, [
                        page.locator('form[action*="/login"]').last.locator('input[name*="token" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[id*="token" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[name*="otp" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[id*="otp" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[name*="code" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[id*="code" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[placeholder*="token" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[placeholder*="otp" i]'),
                        page.locator('form[action*="/login"]').last.locator('input[placeholder*="code" i]'),
                        page.get_by_role("textbox", name=re.compile(r"token|otp|code|kód", re.I)),
                    ], "", timeout=45000)
                except _PwTimeout:
                    _vipa_set_stage("token-field-timeout", page)
                    body = ""
                    try:
                        body = (await page.locator("body").inner_text(timeout=3000))[:700]
                    except Exception:
                        pass
                    log.warning("Vipa OTP token field timeout. body=%r", body)
                    vipa_login_state["error"] = (
                        "Az OTP e-mail küldése sikertelen – Token mező nem jelent meg. "
                        f"Állapot: {vipa_login_state.get('stage')}, URL: {vipa_login_state.get('last_url') or 'ismeretlen'}"
                    )
                    return
                except Exception as exc:
                    _vipa_set_stage("token-field-error", page)
                    log.exception("Vipa OTP token field detection failed: %s", exc)
                    vipa_login_state["error"] = (
                        "Az OTP e-mail küldése sikertelen – Token mező nem jelent meg. "
                        f"Állapot: {vipa_login_state.get('stage')}, hiba: {exc}"
                    )
                    return

                _vipa_set_stage("token-field-ready", page)
                if vipa_login_state.get("ready_event"):
                    vipa_login_state["ready_event"].set()
                log.info("Vipa OTP email sent, waiting for token from buyer (max 10 min)")

                # Block here until the buyer submits the token
                try:
                    await asyncio.wait_for(
                        vipa_login_state["token_event"].wait(), timeout=600
                    )
                except asyncio.TimeoutError:
                    vipa_login_state["error"] = "OTP token nem érkezett be 10 percen belül."
                    return

                token = (vipa_login_state["token"] or "").strip()
                if not token:
                    vipa_login_state["error"] = "Üres token érkezett."
                    return

                token_form = page.locator('form[action*="/login"]').last
                await _fill_first_input(page, [
                    token_form.locator('input[name="token" i]'),
                    token_form.locator('input[name*="token" i]'),
                    token_form.locator('input[id*="token" i]'),
                    token_form.locator('input[name*="otp" i]'),
                    token_form.locator('input[id*="otp" i]'),
                    token_form.locator('input[name*="code" i]'),
                    token_form.locator('input[id*="code" i]'),
                    token_form.locator('input[placeholder*="token" i]'),
                    token_form.locator('input[placeholder*="otp" i]'),
                    token_form.locator('input[placeholder*="code" i]'),
                    page.get_by_role("textbox", name=re.compile(r"token|otp|code|kód", re.I)),
                ], token, timeout=5000)
                _vipa_set_stage("token-filled", page)
                try:
                    await _click_first_button(page, [
                        token_form.locator('input[type="submit"]'),
                        token_form.locator('button[type="submit"]'),
                    ], timeout=5000)
                except Exception as exc:
                    log.warning("Vipa token submit click failed, trying requestSubmit(): %s", exc)
                    await token_form.evaluate("form => form.requestSubmit()")
                _vipa_set_stage("token-submitted", page)

                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except _PwTimeout:
                    log.info("Vipa token submit did not reach domcontentloaded in time; continuing with state check")
                await page.wait_for_timeout(1500)

                if not await _vipa_is_logged_in(page):
                    _vipa_set_stage("checking-home-after-token", page)
                    try:
                        await page.goto(_VIPA_HOME, wait_until="domcontentloaded", timeout=20000)
                    except _PwTimeout:
                        log.warning("Vipa home check timed out after token submit")

                if not await _vipa_is_logged_in(page):
                    _vipa_set_stage("token-login-failed", page)
                    body = ""
                    try:
                        body = (await page.locator("body").inner_text(timeout=3000))[:700]
                    except Exception:
                        pass
                    log.warning("Vipa token login failed. url=%s body=%r", page.url, body)
                    vipa_login_state["error"] = (
                        "Vipa bejelentkezés sikertelen – érvénytelen vagy lejárt token?"
                    )
                    return

                await _save_session(context, _VIPA_SESSION)
                _vipa_set_stage("session-saved", page)
                log.info("Vipa OTP login successful, session saved to %s", _VIPA_SESSION)
                vipa_login_state["error"] = None

            finally:
                await browser.close()

    except Exception as exc:
        log.exception("Vipa OTP login task failed: %s", exc)
        vipa_login_state["error"] = str(exc)
    finally:
        vipa_login_state["active"] = False
        if vipa_login_state["done_event"]:
            vipa_login_state["done_event"].set()


def start_vipa_otp_flow() -> dict:
    """
    Kick off the headless OTP login flow if one is not already running.
    Navigates to the Vipa login page, enters the configured email and clicks
    'Log in' — this triggers the OTP email. The background task then waits for
    the token submitted via the complete-login endpoint.

    Returns a dict with ok/message. Safe to call when a flow is already active.
    """
    email = get_runtime_env("SUPPLIER_VIPA_USERNAME", "—")
    if vipa_login_state["active"]:
        return {
            "ok": True,
            "already_running": True,
            "message": f"OTP folyamat már fut. Adja meg a(z) {email} címre érkezett tokent.",
        }

    vipa_login_state["active"]      = True
    vipa_login_state["token"]       = None
    vipa_login_state["error"]       = None
    vipa_login_state["stage"]       = "queued"
    vipa_login_state["last_url"]    = None
    vipa_login_state["token_event"] = asyncio.Event()
    vipa_login_state["ready_event"] = asyncio.Event()
    vipa_login_state["done_event"]  = asyncio.Event()
    vipa_login_state["task"]        = asyncio.create_task(_run_vipa_otp_login())

    return {
        "ok": True,
        "already_running": False,
        "message": f"OTP e-mail elküldve a(z) {email} címre. Ellenőrizze a postaládát, majd írja be a tokent.",
    }


def vipa_session_available() -> bool:
    """Lightweight, non-destructive check used to decide whether the OTP popup
    is needed before a batch run.

    Only looks at whether a saved session file exists and is still fresh by
    timestamp (≤ SESSION_MAX_AGE_HOURS). Does NOT launch a browser and does NOT
    delete anything — so a flaky headless probe can never wipe a good session
    and trigger repeated OTP prompts. The scraper itself still does the real
    validation when it actually runs.
    """
    from browser.session_utils import load_session, session_is_fresh
    from browser.supplier_vipa import (
        SESSION_FILE as _VIPA_SESSION,
        SESSION_MAX_AGE_HOURS as _VIPA_SESSION_MAX_AGE_HOURS,
    )
    session = load_session(_VIPA_SESSION)
    return bool(session and session_is_fresh(session, _VIPA_SESSION_MAX_AGE_HOURS))


async def vipa_session_is_live() -> bool:
    """Return True only when the saved Vipa session is fresh and accepted by the site.

    Heavyweight: launches a headless browser and may invalidate the session.
    Prefer vipa_session_available() for the pre-run popup gating decision.
    """
    from playwright.async_api import async_playwright as _pw
    from browser.session_utils import invalidate_session, load_session, session_is_fresh
    from browser.supplier_vipa import (
        HOME_URL as _VIPA_HOME,
        SESSION_FILE as _VIPA_SESSION,
        SESSION_MAX_AGE_HOURS as _VIPA_SESSION_MAX_AGE_HOURS,
        _is_logged_in as _vipa_is_logged_in,
    )

    session = load_session(_VIPA_SESSION)
    if not session or not session_is_fresh(session, _VIPA_SESSION_MAX_AGE_HOURS):
        if session:
            invalidate_session(_VIPA_SESSION)
        return False

    try:
        async with _pw() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(storage_state=session["state"])
                page = await context.new_page()
                await page.goto(_VIPA_HOME, wait_until="domcontentloaded", timeout=30000)
                is_live = await _vipa_is_logged_in(page)
                if not is_live:
                    invalidate_session(_VIPA_SESSION)
                return is_live
            finally:
                await browser.close()
    except Exception as exc:
        log.warning("Vipa session live check failed: %s", exc)
        return False
