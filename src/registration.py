"""
The browser-side registration flow.

These steps mirror the real VolleyballLife team-registration wizard captured
from a logged-in walkthrough:

    Sign In -> Register Now -> pick division (radiogroup) -> Next
    -> Team Name -> Next -> pick captain + phone -> Next
    -> "Don't have a full roster yet?" -> Continue
    -> check the two agreement boxes -> Add To Cart -> Check Out Now
    -> (payment page) -> HAND OFF to the human.

The bot never enters payment details; reaching the checkout/payment page is
"spot secured" and we stop there.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeout

import config


def _first_event_url() -> str:
    return f"{config.BASE_URL}/event/{config.EVENT_IDS[0]}?tab=information"


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


def _signin_button(page: Page):
    """
    The header "Sign In" button. exact=True avoids also matching the
    "Close sign in dialog" X button; .first avoids the dialog's own submit
    button once the dialog is open.
    """
    return page.get_by_role("button", name="Sign In", exact=True).first


def is_logged_in(page: Page) -> bool:
    """
    Logged-out event pages show a "Sign In" button; logged-in ones don't.
    """
    page.goto(_first_event_url(), wait_until="domcontentloaded")
    try:
        _signin_button(page).wait_for(state="visible", timeout=3000)
        return False
    except PWTimeout:
        return True


def login(page: Page, creds: LoginCreds) -> None:
    """
    Two-step sign-in: email (Enter) then password (Enter). Assumes a page with
    the "Sign In" button; navigates to the event page first if needed.
    """
    if page.get_by_role("button", name="Sign In", exact=True).count() == 0:
        page.goto(_first_event_url(), wait_until="domcontentloaded")

    _signin_button(page).click()
    page.get_by_label("Email").fill(creds.email)
    page.get_by_label("Email").press("Enter")
    page.get_by_label("Password", exact=True).fill(creds.password)
    page.get_by_label("Password", exact=True).press("Enter")

    # Sign-in is done when the dialog closes -- i.e. the password field is gone.
    page.get_by_label("Password", exact=True).wait_for(state="hidden", timeout=15000)


def park_on_event(page: Page, url: str) -> None:
    page.goto(url, wait_until="networkidle")


def _division_locator(page: Page, division: str):
    """
    Match a division in the radiogroup WITHOUT letting "Coed 6's B" also match
    "Coed 6's BB": require the name not to be followed by another letter.
    """
    pattern = re.compile(re.escape(division) + r"(?![A-Za-z])")
    return page.get_by_role("radiogroup").get_by_text(pattern)


def attempt_register(page: Page, division: str) -> str:
    """
    One full attempt for `division`, returning config.STATUS_{CHECKOUT,MISS}.
    Walks the wizard right up to the payment page and stops. Never pays.
    Normal "division full / step missing" outcomes return STATUS_MISS so the
    ladder retries; only unexpected errors are logged.
    """
    t = config.ATTEMPT_TIMEOUT_MS
    phone = os.environ.get("VBL_PHONE", "")
    try:
        # 1) Open the registration wizard.
        page.get_by_role("button", name="Register Now").nth(
            config.REGISTER_BUTTON_INDEX
        ).click(timeout=t)

        # 2) Pick the division, then advance.
        _division_locator(page, division).click(timeout=t)
        page.get_by_role("button", name="Next").click(timeout=t)

        # 3) Team name.
        page.get_by_label("Team Name*").fill(config.TEAM_NAME, timeout=t)
        page.get_by_role("button", name="Next").click(timeout=t)

        # 4) Captain + phone.
        page.get_by_placeholder("Start typing to search").fill(
            config.CAPTAIN_SEARCH, timeout=t
        )
        page.get_by_text(config.CAPTAIN_OPTION).first.click(timeout=t)
        if phone:
            page.get_by_label("Mobile Phone*").fill(phone, timeout=t)
        page.get_by_role("button", name="Next").click(timeout=t)

        # 5) Skip the full-roster step (smart-quote-proof match).
        page.get_by_role(
            "button", name=re.compile(r"Don.?t have a full roster")
        ).click(timeout=t)
        page.get_by_role("button", name="Continue").click(timeout=t)

        # 6) Agreements.
        page.get_by_label("All information is accurate").check(timeout=t)
        page.get_by_label("I understand and agree to the").check(timeout=t)

        # 7) Cart -> checkout. This is where the spot gets held.
        page.get_by_role("button", name="Add To Cart").click(timeout=t)
        page.get_by_role("link", name="Check Out Now").click(timeout=t)

        # 8) Confirm we reached the payment page -> hand off.
        return _confirm_checkout(page)

    except PWTimeout:
        return config.STATUS_MISS
    except Exception as exc:  # noqa: BLE001
        print(f"[attempt] non-fatal error for {division!r}: {exc}")
        return config.STATUS_MISS


def _confirm_checkout(page: Page) -> str:
    """
    We've clicked "Check Out Now"; confirm the payment page loaded. The
    captured flow lands on a form with a Vuetify field input (.v-field__input);
    a checkout/payment URL or a "Payment" heading are backup signals.
    """
    try:
        page.locator(".v-field__input").first.wait_for(
            state="visible", timeout=config.ATTEMPT_TIMEOUT_MS
        )
        return config.STATUS_CHECKOUT
    except PWTimeout:
        pass
    try:
        page.get_by_text(
            re.compile(r"payment|checkout|card number|billing", re.I)
        ).first.wait_for(state="visible", timeout=1500)
        return config.STATUS_CHECKOUT
    except PWTimeout:
        return config.STATUS_MISS
