# Capturing the real selectors (do this BEFORE race day)

The code ships with **placeholder** selectors (every one marked
`# TODO(selector)` in `src/registration.py`) because the site is behind a
login and can't be inspected blind. Replacing them takes ~10 minutes.

## Option A — Playwright codegen (recommended)

```bash
playwright codegen https://volleyballbeach.volleyballlife.com/login
```

A browser + inspector opens. Do the whole flow by hand **on a real open
registration** (or as far as it lets you without paying):

1. Log in.
2. Go to `/event/39809?tab=information`.
3. Find the **Coed 6's BB** row, click its **Register Now**.
4. Select the division / fill anything required, click through to the
   final **Submit / Reserve** button.
5. Note the confirmation text/URL that appears on success.

Codegen prints a selector for every action. Copy the real selectors into the
matching `# TODO(selector)` spots in `src/registration.py`:

| Placeholder location | What to replace it with |
|---|---|
| `is_logged_in` | An element only visible when logged in (avatar/account menu). |
| `login` | The email, password, and submit selectors. |
| `attempt_register` step 1 | The per-division **Register Now** button, scoped to the division row. |
| `attempt_register` step 2 | The final **Submit/Reserve** button. |
| `attempt_register` step 3 | The **success confirmation** signal (banner text or URL). |

## Option B — DevTools capture (REQUIRED for hybrid mode)

We're running **hybrid** (`EXECUTION_MODE = "hybrid"`), so the fast path fires
this raw request directly. Capture it once:

Open DevTools → Network → check "Preserve log", do the registration by hand,
then find the `POST` that actually creates the reservation. Right-click →
Copy as cURL. From it, fill in `config.py`:

| From the captured request | Put it in `config.py` |
|---|---|
| Request URL (e.g. `/api/.../register`) | `API_REGISTER_URL` |
| HTTP method | `API_REGISTER_METHOD` |
| The `divisionId` for BB and B | `DIVISION_IDS` |
| The full JSON body shape | `_payload()` in `src/api_register.py` |
| Auth token location (localStorage key) | `extract_session()` in `src/api_register.py` |
| Success status + response field | `_looks_successful()` in `src/api_register.py` |

Save the raw request to `docs/register-request.txt` for reference (gitignored
if it contains a token).

Until this is captured, the hybrid path can't run — the code will
automatically fall back to the browser flow (Option A), so **do both
captures** before race day. To skip the API entirely, set
`EXECUTION_MODE = "browser"`.

## Sanity-check with a dry run

```bash
python -m src.sniper --dry-run
```

This fires 20s from now and walks the flow **without clicking the final
submit**, so you can watch it log in, park on the event, and locate the
Register Now button for real — without actually reserving anything.
