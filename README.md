# Peakz Padel — Automated Court Booking

Automated weekly padel court booking for **Peakz Padel Nijmegen Westerpark**.

Every Tuesday at 06:00 Amsterdam time it books a court for exactly 4 weeks later,
pays with prepaid credits, and sends you an email confirmation.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [API vs. browser automation](#2-api-vs-browser-automation)
3. [Project structure](#3-project-structure)
4. [Setup — local development](#4-setup--local-development)
5. [Initial discovery — finding your club_guid and API paths](#5-initial-discovery)
6. [Configuration reference](#6-configuration-reference)
7. [How to run](#7-how-to-run)
8. [Deployment](#8-deployment)
9. [Testing plan](#9-testing-plan)
10. [Known risks and failure scenarios](#10-known-risks-and-failure-scenarios)
11. [Keeping the automation alive after site changes](#11-keeping-the-automation-alive)
12. [Payment limitations — what can and cannot be automated](#12-payment-limitations)

---

## 1. Architecture

```
main.py                     ← CLI entry point (run-now | schedule | once-at | discover | status)
config.py                   ← Typed config, loaded 100% from environment variables
padel_booking/
  engine.py                 ← Booking orchestrator (backend-agnostic)
  foys_client.py            ← FOYS REST API client (primary backend)
  browser_client.py         ← Playwright browser automation (fallback backend)
  notifier.py               ← SMTP email notifications
  state.py                  ← SQLite state store (idempotency + audit trail)
  scheduler.py              ← APScheduler wrappers (weekly + one-off)
.github/workflows/
  weekly_booking.yml        ← GitHub Actions workflow
data/
  state.db                  ← SQLite database (auto-created, git-ignored)
  screenshots/              ← Playwright failure screenshots (git-ignored)
```

**Key design choices:**

| Choice | Reason |
|---|---|
| Engine is backend-agnostic | Same booking logic runs whether using REST API or Playwright |
| SQLite state store | Zero-dependency persistent idempotency on a single machine |
| Two schedulers | `ProductionScheduler` (weekly cron) and `OnceScheduler` (TEST_RUN_AT) share the same `booking_job()` |
| SMTP email | Works with any provider; no third-party SDK required |
| Environment variables only | No secrets in code or config files |

---

## 2. API vs. browser automation

**Peakz Padel runs on the FOYS platform** (Focus On Your Sport BV — same company).

### FOYS REST API — confirmed endpoints

| Method | Path | Status |
|---|---|---|
| `POST` | `/pub/v2/token` | ✅ Confirmed (FOYS developer portal) |
| `POST` | `/pub/v2/token/refresh` | ✅ Confirmed |
| `GET`  | `/court-booking/v1/clubs/{guid}/slots` | ⚠️ Guessed — verify with `discover` |
| `POST` | `/court-booking/v1/bookings` | ⚠️ Guessed — verify with `discover` |
| `GET`  | `/finance/v1/wallet` | ⚠️ Guessed — verify with `discover` |

Authentication is JWT Bearer (1-hour access token, 30-day refresh token).
The `club_guid` is a UUID scoped to the specific Peakz Padel location and is
required for the login request.

**You must run `python main.py discover` once to find your club_guid and the
actual API paths before the REST API mode will work.**

### Playwright fallback

Set `USE_API=false` in `.env` to bypass the REST API entirely and drive the
real website. This is slower and slightly more fragile (UI changes break it)
but is guaranteed to work without any endpoint discovery.

**Recommendation:** start with `USE_API=false` for your first live test.
Once you've confirmed the automation works, run `discover` to get the API
paths and switch to `USE_API=true` for lower latency and higher reliability.

---

## 3. Project structure

```
padelbooking/
├── main.py
├── config.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── padel_booking/
│   ├── __init__.py
│   ├── engine.py
│   ├── foys_client.py
│   ├── browser_client.py
│   ├── notifier.py
│   ├── state.py
│   └── scheduler.py
├── .github/
│   └── workflows/
│       └── weekly_booking.yml
└── data/                   ← git-ignored; created at runtime
    ├── state.db
    └── screenshots/
```

---

## 4. Setup — local development

### Prerequisites

- Python 3.11 or 3.12
- A Peakz Padel account with prepaid credits loaded

### Install

```bash
cd padelbooking
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Configure

```bash
cp .env.example .env
# Edit .env — at minimum set:
#   PEAKZ_EMAIL, PEAKZ_PASSWORD
#   SMTP_USER, SMTP_PASSWORD, NOTIFICATION_TO
#   PEAKZ_CLUB_GUID  (see § 5 below)
```

---

## 5. Initial discovery

### Step 1 — Find your club_guid

The FOYS API requires a `club_guid` UUID for authentication. Run the
interactive discovery tool:

```bash
python main.py discover
```

This opens a headed Chrome browser on the Peakz Padel booking page.
Log in and navigate through the booking flow. The tool intercepts all
API calls to `api.foys.io` and prints:

- The `club_guid` value  →  add as `PEAKZ_CLUB_GUID=` in `.env`
- All endpoint paths     →  add as `FOYS_SLOTS_PATH=`, `FOYS_BOOKING_PATH=`,
                            `FOYS_CREDITS_PATH=` in `.env`

If `discover` doesn't find a `club_guid` automatically, open Chrome DevTools
(F12 > Network > filter "api.foys.io"), click the booking widget, and look
for a POST to `/pub/v2/token` — the `club_guid` is in the request body.

### Step 2 — Verify with a dry run

```bash
DRY_RUN=true USE_API=false python main.py run-now
```

Watch the Playwright browser step through login → calendar → slot selection.
Fix any selector issues before running live (see § 11).

---

## 6. Configuration reference

All settings are environment variables. Copy `.env.example` to `.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PEAKZ_EMAIL` | ✅ | — | Your Peakz account email |
| `PEAKZ_PASSWORD` | ✅ | — | Your Peakz account password |
| `PEAKZ_CLUB_GUID` | For API mode | — | UUID for Nijmegen Westerpark (from `discover`) |
| `PEAKZ_CLUB_NAME` | — | Peakz Padel Nijmegen Westerpark | Used in notifications |
| `PREFERRED_TIMES` | — | `20:00,19:30,19:00` | Slot preference order |
| `DURATION_MINUTES` | — | `90` | Court booking duration |
| `BOOKING_ADVANCE_WEEKS` | — | `4` | Weeks ahead to book |
| `MIN_CREDIT_BALANCE` | — | `20.0` | EUR — skip booking if below this |
| `SMTP_HOST` | — | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | — | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` | ✅ | — | SMTP username |
| `SMTP_PASSWORD` | ✅ | — | SMTP password / app password |
| `SMTP_FROM` | — | Same as `SMTP_USER` | Sender address |
| `NOTIFICATION_TO` | ✅ | — | Your notification email address |
| `USE_API` | — | `true` | `true` = REST API, `false` = Playwright |
| `FOYS_BASE_URL` | — | `https://api.foys.io` | FOYS API base |
| `FOYS_SLOTS_PATH` | — | auto-probe | Path from `discover` |
| `FOYS_BOOKING_PATH` | — | auto-probe | Path from `discover` |
| `FOYS_CREDITS_PATH` | — | auto-probe | Path from `discover` |
| `PEAKZ_BOOKING_URL` | — | `https://peakzpadel.nl/reserveren` | Playwright start URL |
| `DRY_RUN` | — | `false` | Skip actual booking POST; send `[DRY RUN]` notifications |
| `TEST_MODE` | — | `false` | Verbose logging, relaxes Tuesday-only guard |
| `FORCE_RUN_NOW` | — | `false` | Bypass "already ran today" idempotency check |
| `TEST_RUN_AT` | — | — | ISO datetime for `once-at` command (e.g. `2026-03-24T10:00:00`) |
| `BOOKING_DATE_OVERRIDE` | — | — | ISO date to override the target booking date |
| `STATE_DB_PATH` | — | `data/state.db` | SQLite file path |
| `SCREENSHOTS_DIR` | — | `data/screenshots` | Failure screenshot directory |
| `PLAYWRIGHT_HEADED` | — | — | Set to `true` to watch the browser |

### Gmail setup

1. Enable 2-factor authentication on your Google account
2. Go to myaccount.google.com > Security > App passwords
3. Create an app password for "Mail"
4. Use that 16-character password as `SMTP_PASSWORD`

---

## 7. How to run

### Dry run (safe — no booking, no payment)

```bash
DRY_RUN=true TEST_MODE=true python main.py run-now
```

Goes through the full flow (login, slot search, credit check) but skips the
final booking POST. Sends a `[DRY RUN]` email so you can verify notifications.

### Live run today (with Playwright, headed — watch it happen)

```bash
TEST_MODE=true FORCE_RUN_NOW=true USE_API=false PLAYWRIGHT_HEADED=true \
  python main.py run-now
```

### Live run today (with REST API, after discovery)

```bash
TEST_MODE=true FORCE_RUN_NOW=true USE_API=true python main.py run-now
```

### Schedule a one-off test at 10:00 today

```bash
TEST_RUN_AT=2026-03-24T10:00:00 python main.py once-at
```

The process waits until 10:00 Amsterdam time, then runs the booking job
and exits.

### Start the production weekly scheduler

```bash
python main.py schedule
```

Blocks forever. Fires every Tuesday 06:00 Amsterdam. Handles DST automatically.
Run inside `screen`, `tmux`, or as a `systemd` service (see § 8).

### Check recent booking history

```bash
python main.py status
```

---

## 8. Deployment

### Option A — VPS + systemd (recommended for reliability)

**Pros:** DST-exact scheduling, persistent state DB, full log history, no
time limits, runs even if GitHub is down.
**Cons:** Requires a VPS (~€5/month at Hetzner/DigitalOcean).

#### systemd service

```ini
# /etc/systemd/system/padel-booking.service
[Unit]
Description=Peakz Padel weekly booking scheduler
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/padelbooking
EnvironmentFile=/home/youruser/padelbooking/.env
ExecStart=/home/youruser/padelbooking/.venv/bin/python main.py schedule
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable padel-booking
sudo systemctl start padel-booking
sudo journalctl -u padel-booking -f   # follow logs
```

### Option B — GitHub Actions (free, zero infrastructure)

**Pros:** Free for personal repos, managed infrastructure, built-in secret
storage, manual trigger via UI.
**Cons:** UTC-only cron (1 h off in winter), ephemeral state (no persistent
SQLite between runs), 6-hour job timeout.

The workflow at `.github/workflows/weekly_booking.yml` is ready to use.

#### Setup steps

1. Push this repository to GitHub (private repo recommended)
2. Go to Settings > Secrets and variables > Actions
3. Add these secrets:
   - `PEAKZ_EMAIL`
   - `PEAKZ_PASSWORD`
   - `PEAKZ_CLUB_GUID`
   - `SMTP_USER`
   - `SMTP_PASSWORD`
   - `SMTP_FROM`
   - `NOTIFICATION_TO`
   - `USE_API` (set to `true` or `false`)
   - `FOYS_SLOTS_PATH` (from `discover`, if `USE_API=true`)
   - `FOYS_BOOKING_PATH` (from `discover`, if `USE_API=true`)
   - `FOYS_CREDITS_PATH` (from `discover`, if `USE_API=true`)
4. Enable Actions in the repository settings
5. Manually trigger "Peakz Padel — Weekly Booking" with `dry_run=true` to test

> **Note on GitHub Actions state:** the SQLite state DB is not persisted
> between workflow runs. Duplicate-booking protection relies on (a) GH Actions
> only running the cron once per week, and (b) the FOYS backend rejecting
> duplicate reservation attempts.

### Option C — cron on VPS (simpler than systemd, less reliable restarts)

```bash
# crontab -e
# Run every Tuesday at 06:05 Amsterdam time
5 6 * * 2 TZ=Europe/Amsterdam /home/youruser/padelbooking/.venv/bin/python \
    /home/youruser/padelbooking/main.py run-now >> /var/log/padel-booking.log 2>&1
```

---

## 9. Testing plan

### Phase 1 — connectivity and credentials (no booking)

```bash
# 1. Verify SMTP works
DRY_RUN=true TEST_MODE=true python main.py run-now
# → You should receive a [DRY RUN] email

# 2. Verify FOYS auth works (check logs for "Authenticated")
USE_API=true TEST_MODE=true python main.py run-now
```

### Phase 2 — full dry run (Playwright, headed)

```bash
DRY_RUN=true TEST_MODE=true FORCE_RUN_NOW=true USE_API=false \
  PLAYWRIGHT_HEADED=true python main.py run-now
```

Watch the browser step through:
- Login ✓
- Calendar navigation ✓
- Time slot selection ✓
- Credit balance check ✓
- Payment selection ✓
- (stops before confirm in dry-run mode)

### Phase 3 — live booking test

Pick a test booking date where you *want* to book (or that you'll cancel):

```bash
FORCE_RUN_NOW=true USE_API=false PLAYWRIGHT_HEADED=true \
  BOOKING_DATE_OVERRIDE=2026-04-07 python main.py run-now
```

Verify:
- Email confirmation received ✓
- Booking visible in Peakz app ✓
- Credit balance reduced ✓
- `python main.py status` shows `success` ✓

### Phase 4 — idempotency check

Run the same command again immediately:

```bash
BOOKING_DATE_OVERRIDE=2026-04-07 python main.py run-now
```

Expected: logs `"already has a confirmed booking — skipping"`, no second booking.

### Phase 5 — scheduled test

```bash
TEST_RUN_AT=2026-03-24T10:05:00 python main.py once-at
```

Verify the scheduler fires at 10:05 and the job runs.

---

## 10. Known risks and failure scenarios

| Risk | Likelihood | Mitigation |
|---|---|---|
| FOYS updates their API paths | Medium | `discover` re-runs; Playwright fallback |
| FOYS redesigns their booking widget HTML | Medium | Playwright selectors need updating (§ 11) |
| Token expires mid-booking | Low | Token auto-refreshed before each call |
| Site is down at 06:00 | Low | `misfire_grace_time=3600` in APScheduler re-fires up to 1 h late |
| Playwright can't find a selector | Medium | Run headed, check screenshots in `data/screenshots/` |
| Credits balance is 0 | Expected | Notification sent; no booking attempted |
| All 3 preferred times unavailable | Possible | Notification sent; book manually |
| iDEAL / 3DS payment required | Not handled | Credits-only automation; see § 12 |
| Duplicate booking (double payment) | Very low | Idempotency check in state DB + FOYS backend deduplication |
| SMTP fails (notification) | Low | Notification errors are caught and logged; booking still completes |
| VPS is rebooted at 05:59 | Low | systemd `Restart=on-failure`; APScheduler `misfire_grace_time` |

---

## 11. Keeping the automation alive after site changes

### If Playwright selectors break

1. Run with `PLAYWRIGHT_HEADED=true` to see what the browser sees
2. Screenshots are saved to `data/screenshots/` — look at the numbered ones
3. Open Chrome DevTools on the booking page, find the correct selector
4. Update the relevant `_SEL_*` constant in `padel_booking/browser_client.py`
5. Re-run dry mode to verify

### If FOYS API endpoints change

1. Run `python main.py discover` again
2. Update `FOYS_SLOTS_PATH`, `FOYS_BOOKING_PATH`, `FOYS_CREDITS_PATH` in `.env`

### Monthly health check (recommended)

Add a calendar reminder for the first Tuesday of each month:

```bash
DRY_RUN=true FORCE_RUN_NOW=true python main.py run-now
```

If you get the `[DRY RUN]` email, the automation is healthy.

---

## 12. Payment limitations — what can and cannot be automated

### What IS automated

- ✅ Login to your Peakz / FOYS account
- ✅ Checking your prepaid credit balance
- ✅ Booking the court using prepaid credits
- ✅ Confirming the booking

### What is NOT automated

| Method | Reason |
|---|---|
| **iDEAL** | Requires redirect to your bank app / website. Requires 2FA or app approval. Not automatable. |
| **Credit card (3D Secure)** | Requires 3DS redirect, SMS code, or banking app approval. Not automatable. |
| **Topping up credits** | Credit purchase uses Mollie (iDEAL or card). Same limitations as above. |

### Recommendation

Keep your Peakz credit balance above `MIN_CREDIT_BALANCE` (default: €20).
You'll receive an email notification if the balance drops below the threshold
and no booking is made.

Set `MIN_CREDIT_BALANCE` to slightly above the cost of a single 90-minute
session so the automation always has enough credits.

Typical Peakz pricing: €12–€18 per 90-minute court (varies by time/day).
A credit top-up of €100 lasts roughly 5–8 weeks of weekly bookings.

---

## Appendix — FOYS API reference

Based on the FOYS developer portal (developers.foys.tech) and community
reverse-engineering efforts.

**Base URL:** `https://api.foys.io`

**Auth token request:**
```http
POST /pub/v2/token
Content-Type: application/json

{
  "username": "your@email.com",
  "password": "yourpassword",
  "club_guid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

All subsequent requests use `Authorization: Bearer <access_token>`.
