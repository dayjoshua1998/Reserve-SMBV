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
    Run one attempt for `target`, returns (won, how, current_event).

    Hybrid order: try the raw-HTTP register first (no navigation, fastest);
    if that path is unavailable or errors, fall back to the browser flow.
    `current_event` tracks which event page the browser is parked on so we
    only navigate when we must.
    """
    if dry_run:
        return False, "dry", current_event

    # --- Fast path: raw HTTP ---
    if config.EXECUTION_MODE == "hybrid" and http_client is not None:
        try:
            won = api_register.register_via_api(
                http_client, session, target.event_id, target.division
            )
            if won:
                return True, "api", current_event
            # A clean miss from the API still counts as an attempt; only fall
            # through to the browser when the API path can't be used at all.
            return False, "api", current_event
        except Exception as exc:  # noqa: BLE001 -- e.g. no captured division id
            # Fall back to the browser flow for this and subsequent attempts.
            print(f"[hybrid] API path unusable ({exc}); using browser flow.")

    # --- Fallback path: browser ---
    if current_event != target.event_id:
        try:
            page.goto(target.url, wait_until="domcontentloaded")
            current_event = target.event_id
        except Exception as exc:  # noqa: BLE001
            print(f"[browser] nav to {target.event_id} failed: {exc}")
            return False, "browser", None
    else:
        page.reload(wait_until="domcontentloaded")

    won = attempt_register(page, target.division)
    return won, "browser", current_event


def run(dry_run: bool = False) -> int:
    fire_epoch = config.FIRE_TIME.timestamp()
    if dry_run:
        fire_epoch = time.time() + 20  # fire 20s from now for testing
        log("DRY RUN: firing in 20s, will NOT click submit.")

    # 1) Sync the clock up front (and again just before firing).
    offset = timesync.measure_offset(
        config.TIME_SYNC_METHOD, config.NTP_SERVER, f"{config.BASE_URL}/"
    )
    log(f"Time sync: {timesync.describe_offset(offset)}")

    creds = LoginCreds.from_env()
    targets = config.attempt_plan()
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
        session = None
        http_client = None
        if config.EXECUTION_MODE == "hybrid":
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
                won, how, current_event_url = _attempt(
                    target=target,
                    page=page,
                    http_client=http_client,
                    session=session,
                    dry_run=dry_run,
                    current_event=current_event_url,
                )
                dt_ms = (time.perf_counter() - t0) * 1000
                status = "SUCCESS" if won else "miss"
                log(f"#{attempt_no} {target.event_id}:{target.division} "
                    f"[{how}] -> {status} ({dt_ms:.0f}ms)")

                if won:
                    won_events.add(target.event_id)
                    if config.WIN_CONDITION == "one_spot":
                        log(f"Got a spot in event {target.event_id}. Done.")
                        _keep_open(context)
                        return 0
                    if won_events.issuperset(config.EVENT_IDS):
                        log("Got a spot in every event. Done.")
                        _keep_open(context)
                        return 0

                time.sleep(config.RETRY_DELAY_MS / 1000)

        log("Gave up after retry window. No spot secured.")
        _keep_open(context)
        return 1


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
    args = parser.parse_args()

    # Load .env if python-dotenv is installed (optional convenience).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
