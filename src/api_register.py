"""
Raw-HTTP register path (the fast half of "hybrid").

Playwright owns login and keeps the session valid; this module borrows that
session's cookies + auth token and fires the register request directly, which
is dramatically faster than driving the DOM at T-0.

>>> The request shape here is a PLACEHOLDER. <<<
Capture the real register POST from your logged-in browser (docs/CAPTURE.md,
Option B) and fill in API_REGISTER_URL, the JSON body, and DIVISION_IDS in
config.py. Everything that needs a captured value is marked `# TODO(api)`.
"""

from __future__ import annotations

from typing import Any

import httpx
from playwright.sync_api import BrowserContext

import config


def extract_session(context: BrowserContext) -> dict[str, Any]:
    """
    Pull the auth material out of the logged-in Playwright context so httpx
    can reuse it: cookies for the domain, plus any bearer token the SPA stores
    in localStorage.
    """
    cookies = {c["name"]: c["value"] for c in context.cookies()}

    token = None
    try:
        page = context.pages[0]
        # TODO(api): confirm the localStorage key the app uses for its token
        # (common names: 'token', 'access_token', 'auth', 'jwt'). Inspect
        # Application -> Local Storage in DevTools after logging in.
        token = page.evaluate(
            "() => localStorage.getItem('token') "
            "|| localStorage.getItem('access_token') "
            "|| localStorage.getItem('jwt')"
        )
    except Exception:  # noqa: BLE001
        pass

    return {"cookies": cookies, "token": token}


def _headers(session: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": config.BASE_URL,
    }
    if session.get("token"):
        headers["Authorization"] = f"Bearer {session['token']}"
    return headers


def _payload(event_id: int, division: str) -> dict[str, Any]:
    """
    Build the register request body.

    TODO(api): replace with the exact JSON your captured request sends. The
    keys below are guesses purely so the module is runnable end-to-end.
    """
    division_id = config.DIVISION_IDS.get(division)
    return {
        "eventId": event_id,
        "divisionId": division_id,
        # ...any other required fields from the captured request...
    }


def register_via_api(
    client: httpx.Client,
    session: dict[str, Any],
    event_id: int,
    division: str,
) -> str:
    """
    Fire the raw register request. Returns one of config.STATUS_*.

    A successful API grab that creates a cart/order returns STATUS_CHECKOUT so
    the orchestrator can bring the browser to the payment page for handoff; a
    fully-confirmed (no payment) response returns STATUS_CONFIRMED. Ordinary
    "full / rejected" responses return STATUS_MISS. Raises only on transport
    errors or a missing capture, so the orchestrator can fall back to browser.
    """
    if config.DIVISION_IDS.get(division) is None:
        # No captured division id yet -> can't use the API path; signal the
        # caller to use the browser fallback instead.
        raise RuntimeError(
            f"No DIVISION_IDS entry for {division!r}; capture it first "
            "(docs/CAPTURE.md) or run EXECUTION_MODE='browser'."
        )

    url = config.API_REGISTER_URL.format(event_id=event_id)
    resp = client.request(
        config.API_REGISTER_METHOD,
        url,
        json=_payload(event_id, division),
        headers=_headers(session),
        cookies=session.get("cookies"),
        timeout=config.ATTEMPT_TIMEOUT_MS / 1000,
    )

    # TODO(api): tighten this to the real success signals (status + body field).
    if resp.status_code in (200, 201):
        body = _safe_json(resp)
        if _looks_successful(body):
            # If the response indicates a free/instant confirmation, say so;
            # otherwise assume a payment step is required (hand off).
            if _is_free_confirmation(body):
                return config.STATUS_CONFIRMED
            return config.STATUS_CHECKOUT
    return config.STATUS_MISS


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _looks_successful(body: Any) -> bool:
    """TODO(api): match the real success field(s) from the captured response."""
    if not isinstance(body, dict):
        return False
    if body.get("success") is True:
        return True
    if body.get("status") in ("registered", "confirmed", "complete", "pending_payment"):
        return True
    if body.get("registrationId") or body.get("reservationId") or body.get("cartId"):
        return True
    return False


def _is_free_confirmation(body: Any) -> bool:
    """
    True when the response says we're fully registered with nothing left to pay.
    TODO(api): match the real field. Defaulting to False means we assume a
    payment step and hand off -- the safe choice.
    """
    if not isinstance(body, dict):
        return False
    if body.get("status") in ("registered", "confirmed", "complete"):
        return True
    amount_due = body.get("amountDue", body.get("balance"))
    if amount_due is not None:
        try:
            return float(amount_due) == 0.0
        except (TypeError, ValueError):
            return False
    return False
