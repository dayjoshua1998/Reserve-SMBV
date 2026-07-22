"""
Central configuration for the registration sniper.

Everything you are likely to change lives here. Treat this file as the
control panel; the code in src/ reads from it and should rarely need edits
once the real selectors are captured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# WHEN to fire
# ---------------------------------------------------------------------------
# "9:00:00 CST" on July 22, 2026. Central time is on DAYLIGHT time (CDT,
# UTC-5) in July, so the correct wall-clock target is 9:00 AM in the
# America/Chicago zone. ZoneInfo handles the DST offset for us so we don't
# have to hardcode UTC-5 vs UTC-6 -- just say "9 AM Chicago time".
TARGET_TZ = ZoneInfo("America/Chicago")
FIRE_TIME: datetime = datetime(2026, 7, 22, 9, 0, 0, tzinfo=TARGET_TZ)

# Start warming up (open browser, log in, park on the event page) this many
# seconds before FIRE_TIME so at T-0 we are one action away from submitting.
WARMUP_LEAD_SECONDS = 120

# How long after FIRE_TIME to keep retrying before giving up (spots can open
# a few seconds late, or a competitor's cart can time out).
GIVE_UP_AFTER_SECONDS = 180


# ---------------------------------------------------------------------------
# WHAT to register for
# ---------------------------------------------------------------------------
BASE_URL = "https://volleyballbeach.volleyballlife.com"

EVENT_IDS = [39809, 39810]

# Division labels EXACTLY as they appear on the event page. Order = priority.
DIVISION_PRIORITY = [
    "Coed 6's BB",
    "Coed 6's B",
]

# Win condition:
#   "one_spot"   -> stop the instant any registration succeeds.
#   "each_event" -> keep going until we have a spot in every event in EVENT_IDS.
WIN_CONDITION = "each_event"


# ---------------------------------------------------------------------------
# Payment handoff
# ---------------------------------------------------------------------------
# The bot NEVER enters payment details. When an attempt reaches the
# checkout/payment page, that counts as "spot secured" -- the bot stops
# racing that event, alerts you loudly, and leaves the browser parked on the
# payment page for YOU to finish by hand. (Most systems hold the spot for a
# few minutes at checkout.)
STOP_AT_PAYMENT = True

# Audible alert on handoff so you don't miss it if you've stepped away.
ALERT_SOUND = True

# Attempt-result statuses used across the browser + API paths.
STATUS_CONFIRMED = "confirmed"   # fully registered, no payment needed
STATUS_CHECKOUT = "checkout"     # spot held at payment page -> hand off to human
STATUS_MISS = "miss"             # normal miss, keep racing


@dataclass(frozen=True)
class Target:
    """A single (event, division) we can attempt."""
    event_id: int
    division: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/event/{self.event_id}?tab=information"


def attempt_plan() -> list[Target]:
    """
    Build the ordered list of attempts.

    Order encodes the user's ladder:
      1. Coed 6's BB
      2. Coed 6's B (if BB fails)
      3. ...then keep alternating between the two events / divisions.

    The sniper CYCLES this list (skipping already-won events under the
    'each_event' rule) until success or give-up, so listing each target once
    here is enough -- the loop provides the "alternate back and forth".
    """
    plan: list[Target] = []
    # Division-major so the highest-priority division is tried on BOTH events
    # before we drop to the next division. Flip the loop nesting if you'd
    # rather exhaust one event fully before touching the other.
    for division in DIVISION_PRIORITY:
        for event_id in EVENT_IDS:
            plan.append(Target(event_id=event_id, division=division))
    return plan


# ---------------------------------------------------------------------------
# Hybrid mode (raw-HTTP register)
# ---------------------------------------------------------------------------
# Playwright logs in and holds a valid session; at T-0 we fire the raw
# register request directly for minimum latency, falling back to clicking
# through the browser if the API call errors.
#
#   "hybrid"  -> try raw HTTP first, fall back to the browser flow.
#   "browser" -> browser flow only (use this until the API request is captured).
EXECUTION_MODE = "hybrid"

# The register endpoint + payload are UNKNOWN until captured from your logged-in
# browser (see docs/CAPTURE.md, Option B). Fill these in from that capture.
# {event_id} and {division_id} are substituted per attempt.
API_REGISTER_URL = f"{BASE_URL}/api/TODO/register"        # TODO(api): real path
API_REGISTER_METHOD = "POST"

# Map each division LABEL to the internal id the API expects. Get these from
# the captured request / the event page's network calls.
DIVISION_IDS: dict[str, int | None] = {
    "Coed 6's BB": None,   # TODO(api): real division id
    "Coed 6's B": None,    # TODO(api): real division id
}

# Concurrency guard: max simultaneous in-flight register calls across the
# ladder. 1 keeps behavior sequential and polite; raise cautiously.
API_MAX_INFLIGHT = 1


# ---------------------------------------------------------------------------
# Speed / retry tuning
# ---------------------------------------------------------------------------
# Delay between attempts when one fails/misses. Keep small but non-zero to
# avoid hammering the server into rate-limiting you.
RETRY_DELAY_MS = 250

# Per-attempt hard cap. If a single attempt (click -> select -> submit)
# takes longer than this, abandon it and move to the next target.
ATTEMPT_TIMEOUT_MS = 8000


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
# headless=False lets you WATCH it on the day and take over manually if
# something looks off. Flip to True for an unattended/server run.
HEADLESS = False
SLOW_MO_MS = 0  # >0 only for debugging; keep 0 for the real run.

# Persist the logged-in session between runs so login isn't on the hot path.
USER_DATA_DIR = ".browser-profile"


# ---------------------------------------------------------------------------
# Time sync
# ---------------------------------------------------------------------------
# Correct local clock drift against a trusted source before firing. Options:
#   "ntp"  -> query an NTP server (most accurate; needs UDP 123 outbound)
#   "http" -> use the site's HTTP Date header (1s resolution; always works)
#   "none" -> trust the local clock (fine if you run NTP/chrony already)
TIME_SYNC_METHOD = "ntp"
NTP_SERVER = "pool.ntp.org"
