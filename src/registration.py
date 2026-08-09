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
    # domcontentloaded is enough to interact; networkidle needlessly waits for
    # every background request to settle.
    page.goto(url, wait_until="domcontentloaded")


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

    Each step is named and logged so the console shows exactly how far it got;
    on the first stuck step it saves a screenshot and returns STATUS_MISS.
    """
    t = config.ATTEMPT_TIMEOUT_MS
    phone = os.environ.get("VBL_PHONE", "")

    steps: list[tuple[str, "callable"]] = [
        ("open Register Now",
         lambda: page.get_by_role("button", name="Register Now")
                 .nth(config.REGISTER_BUTTON_INDEX).click(timeout=t)),
        (f"select division {division!r}",
         lambda: _division_locator(page, division).click(timeout=t)),
        ("Next (after division)",
         lambda: page.get_by_role("button", name="Next").click(timeout=t)),
        ("fill team name",
         lambda: page.get_by_label("Team Name*").fill(config.TEAM_NAME, timeout=t)),
        ("Next (after team name)",
         lambda: page.get_by_role("button", name="Next").click(timeout=t)),
        ("search captain",
         lambda: page.get_by_placeholder("Start typing to search")
                 .fill(config.CAPTAIN_SEARCH, timeout=t)),
        ("pick captain",
         lambda: _pick_captain(page, t)),
    ]
    if phone:
        steps.append(
            ("fill phone (if shown)", lambda: _maybe_fill_phone(page, phone, t)))
    steps += [
        ("Next (after captain)",
         lambda: page.get_by_role("button", name="Next").click(timeout=t)),
        ("skip roster",
         lambda: page.get_by_role(
             "button", name=re.compile(r"Don.?t have a full roster")).click(timeout=t)),
        ("Continue",
         lambda: page.get_by_role("button", name="Continue").click(timeout=t)),
        ("check accuracy box",
         lambda: page.get_by_label("All information is accurate").check(timeout=t)),
        ("check agreement box",
         lambda: page.get_by_label("I understand and agree to the").check(timeout=t)),
        ("Add To Cart",
         lambda: page.get_by_role("button", name="Add To Cart").click(timeout=t)),
        ("Check Out Now",
         lambda: page.get_by_role("link", name="Check Out Now").click(timeout=t)),
    ]

    for desc, fn in steps:
        try:
            fn()
            print(f"[wizard] ok: {desc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[wizard] STUCK at: {desc}  ({type(exc).__name__})", flush=True)
            _dump_failure(page, f"{division}-{desc}")
            return config.STATUS_MISS

    if config.FILL_PAYMENT:
        _fill_payment(page)

    return _confirm_checkout(page)


def _fill_payment(page: Page) -> None:
    """
    Best-effort fill of the checkout payment form, then STOP -- never submits.
    The card fields are hosted by Stripe in a cross-origin iframe (the recorder
    can't see inside it, but Playwright can fill it at runtime). The iframe name
    is randomized per load, so we match it by its stable prefix.
    """
    # Receipt email -- a normal page field, not inside the Stripe frame.
    receipt = os.environ.get("VBL_RECEIPT_EMAIL") or os.environ.get("VBL_EMAIL", "")
    if receipt:
        try:
            page.get_by_label(re.compile(r"Email Receipt", re.I)).fill(
                receipt, timeout=4000)
            print("[pay] receipt email filled", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[pay] receipt email skipped: {exc}", flush=True)

    number = os.environ.get("VBL_CARD_NUMBER", "")
    if not number:
        print("[pay] no VBL_CARD_NUMBER in .env -- leaving payment blank "
              "for manual entry", flush=True)
        return

    exp = os.environ.get("VBL_CARD_EXP", "")
    cvv = os.environ.get("VBL_CARD_CVV", "")
    zipc = os.environ.get("VBL_CARD_ZIP", "")

    # Stripe may use one combined iframe or several; try each matching frame.
    stripe_frames = page.frame_locator("iframe[name^='__privateStripeFrame']")

    def fill_stripe(desc: str, patterns: list[str], value: str) -> None:
        if not value:
            return
        for pat in patterns:
            try:
                stripe_frames.first.get_by_placeholder(
                    re.compile(pat, re.I)).fill(value, timeout=3000)
                print(f"[pay] filled {desc}", flush=True)
                return
            except Exception:  # noqa: BLE001
                continue
        print(f"[pay] could NOT find {desc} field (may need a tweak)", flush=True)

    fill_stripe("card number", [r"card number"], number)
    fill_stripe("expiry", [r"MM ?/ ?YY", r"expir"], exp)
    fill_stripe("CVC", [r"CVC", r"CVV", r"security"], cvv)
    fill_stripe("ZIP", [r"ZIP", r"postal"], zipc)
    print("[pay] payment fields filled -- STOPPING before Submit Payment. "
          "Click Submit Payment yourself.", flush=True)


def _pick_captain(page: Page, t: int) -> None:
    """
    Click the captain from the search results. Prefer the result that shows the
    name WITH an email (like "Josh Day - d***@gmail.com") so we don't
    accidentally click a bare "Josh Day" heading elsewhere on the page.
    """
    name = config.CAPTAIN_OPTION
    with_email = page.get_by_text(re.compile(re.escape(name) + r".*@"))
    try:
        with_email.first.wait_for(state="visible", timeout=3000)
        with_email.first.click(timeout=t)
        print("[wizard]   (picked captain via name+email result)", flush=True)
        return
    except PWTimeout:
        pass
    # Fallback: plain name match.
    page.get_by_text(name).first.click(timeout=t)
    print("[wizard]   (picked captain via name only)", flush=True)


def _phone_field(page: Page):
    """Find the phone input across a few possible labels/attrs; None if absent."""
    candidates = [
        page.get_by_label("Mobile Phone*"),
        page.get_by_label(re.compile(r"mobile phone", re.I)),
        page.get_by_label(re.compile(r"\bphone\b", re.I)),
        page.get_by_placeholder(re.compile(r"phone", re.I)),
        page.locator("input[type='tel']"),
    ]
    for c in candidates:
        try:
            loc = c.first
            loc.wait_for(state="visible", timeout=1500)
            return loc
        except PWTimeout:
            continue
    return None


def _maybe_fill_phone(page: Page, phone: str, t: int) -> None:
    """
    Fill the phone field if we can find one and it's empty. Some profiles have
    a phone on file (field pre-filled or absent) -- then we just move on.
    """
    field = _phone_field(page)
    if field is None:
        print("[wizard]   (no phone field found -- skipping)", flush=True)
        return
    try:
        if (field.input_value(timeout=1000) or "").strip():
            print("[wizard]   (phone already filled -- leaving as-is)", flush=True)
            return
    except Exception:  # noqa: BLE001
        pass
    field.fill(phone, timeout=t)
    print("[wizard]   (filled phone)", flush=True)


def _dump_failure(page: Page, tag: str) -> None:
    """Save a screenshot + URL of the stuck screen for diagnosis."""
    try:
        os.makedirs("screenshots", exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", tag)[:60]
        path = os.path.join("screenshots", f"stuck_{safe}.png")
        page.screenshot(path=path)
        print(f"[wizard] url={page.url}", flush=True)
        print(f"[wizard] screenshot saved: {path}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[wizard] could not capture failure screen: {exc}", flush=True)


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
