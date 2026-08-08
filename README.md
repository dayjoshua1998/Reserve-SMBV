# Reserve-SMBV

A **registration sniper** for VolleyballLife beach events. At a precise
instant it logs in, grabs a spot in a priority division, and submits as fast
as possible — with a fallback ladder if the top choice is gone.

Target run: **9:00:00 AM CDT, July 22, 2026** for events `39809` and `39810`,
division **Coed 6's BB**, falling back to **Coed 6's B**, alternating between
the two events until a spot lands **in each event**.

**Architecture: hybrid.** Playwright logs in and holds a valid session; at
T-0 the raw register HTTP request is fired directly for minimum latency, with
the browser click-through as an automatic fallback if the API call errors.

> ⚠️ For your own personal registration only. Keep `RETRY_DELAY_MS` sane so
> you don't hammer the site — this is meant to win one spot, not to load-test
> the server.

## How it works

1. **Time sync** — corrects your OS clock drift against NTP (falls back to the
   server's HTTP `Date` header) so T-0 is accurate to the millisecond.
2. **Warmup** (`WARMUP_LEAD_SECONDS` before fire) — opens the browser, reuses
   or creates a login session, and parks on the event page so it's already
   rendered. Login is kept **off the hot path**.
3. **Fire** — at exactly the target instant, runs the attempt ladder:
   `Coed 6's BB` on both events → `Coed 6's B` on both → repeat, until every
   event has a spot or the give-up window closes. Each attempt tries the raw
   HTTP register first and falls back to the browser.
4. **Confirm** — keeps going until a spot is secured in **each** event
   (`WIN_CONDITION = "each_event"`), then leaves the browser open to verify/pay.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env      # then edit with your real login
```

## Prime the login ahead of time (recommended)

So there's **zero login on the hot path**, log in once before the event and
the session is saved to a persistent profile (`.browser-profile/`) that the
real run reuses:

```bash
python -m src.sniper --prime
```

It opens a visible browser. If your `.env` has creds it logs in automatically;
otherwise log in by hand (and clear any 2FA) then press Enter. Re-run it if
the session ever looks stale. **Run it in the same place the real snipe will
run** — the saved session lives on that machine's disk.

## Payment: handed off to you

The bot **never enters card details.** When an attempt reaches the payment /
checkout page it treats the spot as secured, sounds an alert, and leaves the
logged-in browser parked on the payment page for **you** to finish by hand
(`STOP_AT_PAYMENT = True`). Systems typically hold the spot for a few minutes
at checkout — enough time to type a card in.

## ‼️ Before race day: capture the real selectors

The registration flow ships with **placeholder selectors** because the site
is behind a login. You must replace them once — see **[docs/CAPTURE.md](docs/CAPTURE.md)**.
Then verify with a dry run (fires 20s out, never actually submits):

```bash
python -m src.sniper --dry-run
```

## Race day

```bash
python -m src.sniper
```

Start it a few minutes early. It syncs the clock, warms up, and holds until
9:00:00 CDT on its own. Keep the machine awake and on stable internet.

Commands:

| Command | What it does |
|---|---|
| `python -m src.sniper --prime` | Log in ahead of time, save the session, exit. |
| `python -m src.sniper --rehearse` | Dress rehearsal NOW in the browser; stops at the payment screen, never pays. Safe practice run. |
| `python -m src.sniper --dry-run` | Fire 20s out; walk the flow **without** submitting. |
| `python -m src.sniper` | The real run. |

## Configuration

Everything tunable lives in [`config.py`](config.py): fire time & timezone,
event IDs, division priority, win condition (`one_spot` vs `each_event`),
retry delay, headless mode, and time-sync method.

## Layout

```
config.py            # all knobs
src/timesync.py      # clock-drift correction + precise sleep
src/registration.py  # browser flow / fallback (PLACEHOLDER selectors -> CAPTURE.md)
src/api_register.py  # raw-HTTP register (hybrid fast path; PLACEHOLDER request)
src/sniper.py        # orchestrator: warmup -> fire -> ladder (API-first, browser fallback)
docs/CAPTURE.md      # how to capture the real selectors AND the register request
```

## Roadmap

- [ ] Capture the real register request + division ids for the hybrid fast path (required — see CAPTURE.md Option B).
- [ ] Capture the browser-flow selectors for the fallback (required — CAPTURE.md Option A).
- [ ] Desktop/push notification on success/failure.
- [ ] Handle the payment step if the site requires it to hold the spot.
