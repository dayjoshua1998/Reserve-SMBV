"""
The browser-side registration flow.

>>> IMPORTANT <<<
The selectors below are EDUCATED PLACEHOLDERS. This site is behind a login,
so they must be verified against the real page before the run. See
`docs/CAPTURE.md` for exactly how to capture the real selectors in ~10
minutes with `playwright codegen`. Every spot that needs a real value is
marked `# TODO(selector)`.

Each function is intentionally small and returns a boolean/raises so the
orchestrator in sniper.py can time and retry them cheaply.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeout

import config


@dataclass
class LoginCreds:
    email: str
    password: str

    @classmethod
    def from_env(cls) -> "LoginCreds":
        email = os.environ.get("VBL_EMAIL")
        password = os.environ.get("VBL_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "Set VBL_EMAIL and VBL_PASSWORD (see .env.example). "
                "Never hardcode credentials in the repo."
            )
        return cls(email=email, password=password)


def is_logged_in(page: Page) -> bool:
    """
    Cheap check for an existing session so we don't re-login on every run
    when using a persistent profile.
    """
    page.goto(f"{config.BASE_URL}/", wait_until="domcontentloaded")
    # TODO(selector): replace with something only present when authenticated,
    # e.g. an account/avatar menu.
    try:
        page.wait_for_selector(
            "[data-testid='account-menu'], .user-avatar, text=Log Out",
            timeout=3000,
        )
        return True
    except PWTimeout:
        return False


def login(page: Page, creds: LoginCreds) -> None:
    """Perform a fresh login. Kept OFF the hot path via warmup + profile reuse."""
    page.goto(f"{config.BASE_URL}/login", wait_until="domcontentloaded")

    # TODO(selector): confirm these three selectors + the submit button.
    page.fill("input[type='email'], input[name='email']", creds.email)
    page.fill("input[type='password'], input[name='password']", creds.password)
    page.click("button[type='submit'], button:has-text('Log In')")

    # TODO(selector): wait for a post-login signal instead of a fixed sleep.
    page.wait_for_selector(
        "[data-testid='account-menu'], .user-avatar, text=Log Out",
        timeout=15000,
    )


def park_on_event(page: Page, url: str) -> None:
    """
    Navigate to an event page and get it fully rendered during warmup so the
    hot-path attempt starts from a warm page.
    """
    page.goto(url, wait_until="networkidle")


def attempt_register(page: Page, division: str) -> str:
    """
    One full attempt for a single division on the currently-loaded event page:
      Register Now -> pick division -> submit -> reach checkout OR confirmation.

    Returns one of config.STATUS_{CHECKOUT,CONFIRMED,MISS}. Must be fast and
    must NOT raise on the ordinary "division full / button missing" case --
    that's a normal miss we retry, not an error.
    """
    try:
        # 1) Open the registration UI for this division.
        #    Many event pages list divisions each with their own "Register Now".
        #    TODO(selector): scope "Register Now" to the row for `division`.
        division_row = page.locator(
            f"xpath=//*[contains(., \"{division}\")][.//button or .//a]"
        ).filter(has=page.get_by_role("button", name="Register Now")).first

        register_btn = division_row.get_by_role("button", name="Register Now")
        register_btn.wait_for(state="visible", timeout=config.ATTEMPT_TIMEOUT_MS)
        register_btn.click()

        # 2) On the registration modal/page, confirm/select the division if
        #    prompted, then submit.
        #    TODO(selector): the real confirm/submit button text.
        submit = page.get_by_role(
            "button", name="Submit"
        ).or_(page.get_by_role("button", name="Reserve")).or_(
            page.get_by_role("button", name="Complete Registration")
        ).first
        submit.wait_for(state="visible", timeout=config.ATTEMPT_TIMEOUT_MS)
        submit.click()

        # 3) Decide the outcome. We race the two success signals against a
        #    "still full / rejected" outcome. Whichever selector resolves first
        #    wins. TODO(selector): make these match the REAL pages.
        return _classify_outcome(page)

    except PWTimeout:
        return config.STATUS_MISS
    except Exception as exc:  # noqa: BLE001
        # Log but treat as a miss so the loop keeps racing.
        print(f"[attempt] non-fatal error for {division!r}: {exc}")
        return config.STATUS_MISS


def _classify_outcome(page: Page) -> str:
    """
    After submitting, work out whether we landed on a payment/checkout page
    (spot held -> hand off), a plain confirmation (done, no payment), or
    neither (miss). TODO(selector): tune all three matchers to the real site.
    """
    # Checkout / payment: the most likely "we grabbed it" state.
    # e.g. a "Payment"/"Checkout" heading or a "Card number"/"Billing" field.
    # These regexes are placeholders until captured.
    checkout = page.get_by_text(
        re.compile(r"payment|checkout|card number|billing", re.I)
    ).first
    # Plain confirmation (free event / no payment step).
    confirmed = page.get_by_text(
        re.compile(r"registered|confirmed|reservation complete", re.I)
    ).first

    # Give the page a moment for one of the signals to appear.
    deadline_ms = config.ATTEMPT_TIMEOUT_MS
    try:
        checkout.wait_for(state="visible", timeout=deadline_ms)
        return config.STATUS_CHECKOUT
    except PWTimeout:
        pass
    try:
        confirmed.wait_for(state="visible", timeout=1000)
        return config.STATUS_CONFIRMED
    except PWTimeout:
        return config.STATUS_MISS
