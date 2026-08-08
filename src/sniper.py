"""
Orchestrator: warm up, sync the clock, fire at T-0, run the fallback ladder.

Run with:  python -m src.sniper        (from the repo root)
Dry run:   python -m src.sniper --dry-run   (fire 20s from now, don't submit)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from playwright.sync_api import sync_playwright

import config
from src import api_register, timesync
from src.registration import (
    LoginCreds,
    attempt_register,
    is_logged_in,
    login,
    park_on_event,
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


def _attempt(target, page, http_client, session, dry_run, current_event):
    """
    Run one attempt for `target`, returns (status, how, current_event).

    status is one of config.STATUS_*. Hybrid order: try the raw-HTTP register
    first (no navigation, fastest); if that path is unavailable or errors,
    fall back to the browser flow. `current_event` tracks which event page the
    browser is parked on so we only navigate when we must.
    """
    if dry_run:
        return config.STATUS_MISS, "dry", current_event

    # --- Fast path: raw HTTP ---
    if config.EXECUTION_MODE == "hybrid" and http_client is not None:
        try:
            status = api_register.register_via_api(
                http_client, session, target.event_id, target.division
            )
            return status, "api", current_event
        except Exception as exc:  # noqa: BLE001 -- e.g. no captured division id
            # Fall back to the browser flow for this and subsequent attempts.
            print(f"[hybrid] API path unusable ({exc}); using browser flow.")

    # --- Fallback path: browser ---
    # Always start each attempt from the clean event (info) page, so a retry
    # after a half-finished wizard resets instead of getting lost.
    try:
        page.goto(target.url, wait_until="domcontentloaded")
        current_event = target.event_id
    except Exception as exc:  # noqa: BLE001
        print(f"[browser] nav to {target.event_id} failed: {exc}")
        return config.STATUS_MISS, "browser", None

    status = attempt_register(page, target.division)
    return status, "browser", current_event


def _alert(msg: str) -> None:
    """Loud, hard-to-miss handoff signal (terminal bell + banner)."""
    bell = "\a" if config.ALERT_SOUND else ""
    line = "=" * 60
    print(f"{bell}\n{line}\n  🔔  {msg}\n{line}\n", flush=True)


def _handoff_to_payment(page, target, how) -> None:
    """
    We've secured a spot that needs payment. Make sure the (logged-in) browser
    is on the payment page and hand off to the human. The bot never enters
    card details.
    """
    # If the grab happened over the API, the browser isn't on the checkout page
    # yet -- bring it to the event so the human can complete payment there.
    if how == "api":
        try:
            page.goto(target.url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"[handoff] could not open {target.url}: {exc}")
    _alert(
        f"SPOT SECURED for event {target.event_id} ({target.division}). "
        "Finish PAYMENT in the browser window NOW -- the hold won't last long."
    )


def run(dry_run: bool = False, rehearse: bool = False) -> int:
    fire_epoch = config.FIRE_TIME.timestamp()
    if dry_run:
        fire_epoch = time.time() + 20  # fire 20s from now for testing
        log("DRY RUN: firing in 20s, will NOT click submit.")
    if rehearse:
        fire_epoch = time.time() + 8  # fire almost immediately
        log("REHEARSAL: firing in 8s, browser mode, will STOP at the payment "
            "screen (never pays). Safe to run while already registered -- just "
            "don't complete payment.")

    # 1) Sync the clock up front (and again just before firing).
    offset = timesync.measure_offset(
        config.TIME_SYNC_METHOD, config.NTP_SERVER, f"{config.BASE_URL}/"
    )
    log(f"Time sync: {timesync.describe_offset(offset)}")

    creds = LoginCreds.from_env()
    targets = config.attempt_plan()
    if rehearse:
        # Practice against the open test event instead of the real (full) one.
        targets = [config.Target(config.REHEARSE_EVENT_ID, d)
                   for d in config.DIVISION_PRIORITY]
        log(f"REHEARSAL target: event {config.REHEARSE_EVENT_ID} (test event).")
    log(f"Attempt plan ({len(targets)} targets): "
        + " | ".join(f"{t.event_id}:{t.division}" for t in targets))

    won_events: set[int] = set()

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=config.USER_DATA_DIR,
            headless=config.HEADLESS,
            slow_mo=config.SLOW_MO_MS,
        )
        page = context.pages[0] if context.pages else context.new_page()

        # 2) WARMUP: log in if needed, park on the first event so the page is hot.
        log("Warmup: checking session...")
        if not is_logged_in(page):
            log("Not logged in -- logging in now.")
            login(page, creds)
        else:
            log("Existing session reused.")
        park_on_event(page, targets[0].url)
        log(f"Parked on {targets[0].url}")

        # Borrow the logged-in session for the raw-HTTP hot path.
        # Rehearsal forces the browser path so you can watch every screen.
        session = None
        http_client = None
        if config.EXECUTION_MODE == "hybrid" and not rehearse:
            session = api_register.extract_session(context)
            http_client = httpx.Client(http2=True)
            has_token = bool(session.get("token"))
            log(f"Hybrid: session captured (token={'yes' if has_token else 'no'}, "
                f"{len(session.get('cookies', {}))} cookies).")

        # 3) Re-sync and wait precisely for T-0.
        offset = timesync.measure_offset(
            config.TIME_SYNC_METHOD, config.NTP_SERVER, f"{config.BASE_URL}/"
        )
        seconds_out = fire_epoch - timesync.true_now(offset)
        if seconds_out > 0:
            log(f"Re-synced. Holding {seconds_out:.1f}s until fire time "
                f"({datetime.fromtimestamp(fire_epoch, config.TARGET_TZ)}).")
            timesync.sleep_until(fire_epoch, offset)
        else:
            log(f"Fire time already passed by {-seconds_out:.1f}s -- going now.")

        # 4) FIRE: cycle the ladder until success or give-up.
        log(">>> FIRE <<<")
        deadline = timesync.true_now(offset) + config.GIVE_UP_AFTER_SECONDS
        attempt_no = 0
        current_event_url: int | None = None

        while timesync.true_now(offset) < deadline:
            for target in targets:
                if timesync.true_now(offset) >= deadline:
                    break
                if config.WIN_CONDITION == "each_event" and target.event_id in won_events:
                    continue

                attempt_no += 1
                t0 = time.perf_counter()
                status, how, current_event_url = _attempt(
                    target=target,
                    page=page,
                    http_client=http_client,
                    session=session,
                    dry_run=dry_run,
                    current_event=current_event_url,
                )
                dt_ms = (time.perf_counter() - t0) * 1000
                log(f"#{attempt_no} {target.event_id}:{target.division} "
                    f"[{how}] -> {status} ({dt_ms:.0f}ms)")

                won = status in (config.STATUS_CONFIRMED, config.STATUS_CHECKOUT)
                if won:
                    won_events.add(target.event_id)

                    if status == config.STATUS_CHECKOUT and config.STOP_AT_PAYMENT:
                        # Human finishes payment; don't keep racing this event.
                        _handoff_to_payment(page, target, how)
                        current_event_url = target.event_id
                    else:
                        _alert(f"Registered for event {target.event_id} "
                               f"({target.division}) -- no payment needed.")

                    if config.WIN_CONDITION == "one_spot":
                        log(f"Secured event {target.event_id}. Done.")
                        _keep_open(context)
                        return 0
                    if won_events.issuperset(config.EVENT_IDS):
                        log("Secured a spot in every event. Done.")
                        _keep_open(context)
                        return 0

                time.sleep(config.RETRY_DELAY_MS / 1000)

            if rehearse:
                # One diagnostic pass through the ladder, then stop (don't loop).
                log("Rehearsal pass complete. Review the [wizard] lines above "
                    "to see how far it got.")
                _keep_open(context)
                return 1

        log("Gave up after retry window. No spot secured.")
        _keep_open(context)
        return 1


def prime() -> int:
    """
    Log in AHEAD of race day and persist the session to disk.

    Run this any time before the event (e.g. the night before). It opens the
    browser, establishes a logged-in session in USER_DATA_DIR, verifies it,
    and exits -- so the real run reuses that session with zero login on the
    hot path. If VBL_EMAIL/VBL_PASSWORD are set it logs in automatically;
    otherwise it waits for you to log in by hand.
    """
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=config.USER_DATA_DIR,
            headless=False,  # always visible so you can log in / solve any 2FA
        )
        page = context.pages[0] if context.pages else context.new_page()

        if is_logged_in(page):
            log("Already logged in -- session is primed. ✅")
            context.close()
            return 0

        try:
            creds = LoginCreds.from_env()
            log("Logging in from VBL_EMAIL/VBL_PASSWORD...")
            login(page, creds)
            log("Login succeeded -- session saved to the profile. ✅")
        except RuntimeError:
            _alert("No credentials in .env. Log in by hand in the browser "
                   "window, then press Enter here.")
            input("Press Enter once you're logged in... ")
            if not is_logged_in(page):
                log("Still not detecting a session. Check selectors / try again.")
                context.close()
                return 1
            log("Manual login detected -- session saved. ✅")

        context.close()
        return 0


def _keep_open(context) -> None:
    """
    Leave the browser open on the confirmation so a human can verify /
    complete payment if the site requires it. Ctrl-C to close.
    """
    if config.HEADLESS:
        return
    try:
        log("Leaving browser open for verification. Press Ctrl-C to exit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Volleyball registration sniper")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fire 20s from now and skip the final submit click.")
    parser.add_argument("--prime", action="store_true",
                        help="Log in ahead of time and save the session; then exit.")
    parser.add_argument("--rehearse", action="store_true",
                        help="Full dress rehearsal NOW in the browser; stops at "
                             "the payment screen and never pays. Safe practice run.")
    args = parser.parse_args()

    # Load .env if python-dotenv is installed (optional convenience).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.prime:
        sys.exit(prime())
    sys.exit(run(dry_run=args.dry_run, rehearse=args.rehearse))


if __name__ == "__main__":
    main()
