#!/usr/bin/env python3
"""
Peakz Padel — automated court booking
======================================

Commands
--------
  run-now        Book immediately using today as the trigger date (same code as production).
  schedule       Start the weekly production scheduler (blocks forever).
  once-at        Schedule a one-off run at the time in TEST_RUN_AT env var.
  discover       Open a headed browser, intercept FOYS API traffic, and print
                 the endpoint paths you need for .env.
  status         Print the last 10 state records from the local DB.

Quick-start examples
--------------------
  # Dry run — full flow, no actual booking
  DRY_RUN=true python main.py run-now

  # Live run right now (TEST_MODE=true relaxes the Tuesday-only guard)
  TEST_MODE=true FORCE_RUN_NOW=true python main.py run-now

  # Headed browser so you can watch what happens
  TEST_MODE=true FORCE_RUN_NOW=true USE_API=false PLAYWRIGHT_HEADED=true python main.py run-now

  # One-off test at 10:00 today
  TEST_RUN_AT=2026-03-24T10:00:00 python main.py once-at

  # Discover FOYS API endpoints (required once before USE_API=true works)
  python main.py discover

  # Production weekly scheduler
  python main.py schedule
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Load .env before anything else
try:
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("DOTENV_PATH", ".env"))
except ImportError:
    pass  # python-dotenv is optional; .env loading can be done externally

import structlog

from config import TZ, load_config
from padel_booking.engine import make_engine


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Silence noisy third-party loggers
    for lib in ("httpx", "httpcore", "hpack", "playwright"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
    )


# ---------------------------------------------------------------------------
# Booking job (called by both schedulers and run-now)
# ---------------------------------------------------------------------------

def booking_job() -> None:
    """The canonical booking job. Identical code path for test and production."""
    cfg = load_config()
    _setup_logging(debug=cfg.test_mode)

    log = logging.getLogger("main")

    now_ams = _now_amsterdam()
    trigger_date = now_ams.date()

    # Target date: trigger + 4 weeks (or override)
    if cfg.booking_date_override:
        target_date = cfg.booking_date_override
        log.info("BOOKING_DATE_OVERRIDE active: target=%s", target_date)
    else:
        target_date = trigger_date + timedelta(weeks=cfg.booking_advance_weeks)

    log.info(
        "booking_job: trigger=%s target=%s dry_run=%s test_mode=%s",
        trigger_date, target_date, cfg.dry_run, cfg.test_mode,
    )

    engine = make_engine(cfg)
    result = engine.run(trigger_date=trigger_date, target_date=target_date)

    log.info("Engine result: status=%s", result.status)
    if result.status == "success":
        log.info(
            "Booked: %s %s–%s ref=%s",
            result.court_name, result.start_time, result.end_time,
            result.reservation_reference,
        )
    elif result.status in ("no_slots", "insufficient_credits", "already_booked"):
        log.warning("Run ended without booking: %s", result.status)
    elif result.status == "error":
        log.error("Run failed: %s", result.error_message)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run_now() -> None:
    """Run the booking job immediately."""
    os.environ.setdefault("FORCE_RUN_NOW", "true")
    booking_job()


def cmd_schedule() -> None:
    """Start the production weekly scheduler (blocks)."""
    cfg = load_config()
    _setup_logging(debug=cfg.test_mode)
    log = logging.getLogger("main")
    log.info("Starting PRODUCTION scheduler — every Tuesday 06:00 Amsterdam")

    from padel_booking.scheduler import ProductionScheduler
    ProductionScheduler(booking_job).start()


def cmd_once_at() -> None:
    """Schedule a single run at the time in TEST_RUN_AT env var."""
    cfg = load_config()
    _setup_logging(debug=True)
    log = logging.getLogger("main")

    if not cfg.test_run_at:
        log.error("TEST_RUN_AT is not set. Example: TEST_RUN_AT=2026-03-24T10:00:00")
        sys.exit(1)

    log.info("Scheduling one-off run at %s", cfg.test_run_at)
    from padel_booking.scheduler import OnceScheduler
    OnceScheduler(booking_job, run_at=cfg.test_run_at).start()


def cmd_discover() -> None:
    """
    Open a headed browser on the Peakz Padel booking page, intercept all
    calls to api.foys.io, and print discovered endpoints + club_guid.

    Press Enter in the terminal when you have finished logging in and
    navigating through the booking flow.
    """
    _setup_logging(debug=True)
    log = logging.getLogger("discover")
    cfg = load_config()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright is not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    import re, json as _json

    captured: list[dict] = []

    def _redact(text: str) -> str:
        """Remove password values from form-encoded or JSON bodies."""
        if not text:
            return ""
        text = re.sub(r'(password=)[^&"]+', r'\1***', text)
        text = re.sub(r'("password"\s*:\s*")[^"]+(")', r'\1***\2', text)
        return text

    def on_request(req):
        if "api.foys.io" in req.url or "foys" in req.url.lower():
            captured.append({
                "method": req.method,
                "url": req.url,
                "post_data": req.post_data,
            })

    print("\n" + "=" * 60)
    print("FOYS API ENDPOINT DISCOVERY")
    print("=" * 60)
    print(f"Opening: {cfg.peakz_booking_url}")
    print("Steps to follow:")
    print("  1. Log in with your Peakz account")
    print("  2. Select a date and browse available time slots")
    print("  3. Go up to (but do NOT confirm) the payment step")
    print("  4. Come back to this terminal and press Enter")
    print("=" * 60 + "\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=300)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
        )
        page = ctx.new_page()
        page.on("request", on_request)
        page.goto(cfg.peakz_booking_url)

        input("\n>>> Press Enter when done <<<\n")
        browser.close()

    print("\n" + "=" * 60)
    print("CAPTURED API CALLS")
    print("=" * 60)

    federation_ids: set[str] = set()
    location_ids: set[str] = set()
    UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

    for call in captured:
        body = _redact(call["post_data"] or "")
        print(f"  {call['method']:<6} {call['url']}")
        if body:
            print(f"         body: {body[:200]}")

        # Extract federationId from token POST body
        if "/token" in call["url"] and call["post_data"]:
            m = re.search(r'federationId=([^&]+)', call["post_data"])
            if m:
                federation_ids.add(m.group(1))

        # Extract locationId from slot-search GET URLs
        if "locationId=" in call["url"]:
            m = re.search(r'locationId=([0-9a-f-]{36})', call["url"], re.I)
            if m:
                location_ids.add(m.group(1))

    print("\n" + "=" * 60)
    print("EXTRACTED IDs")
    print("=" * 60)
    if federation_ids:
        for v in federation_ids:
            print(f"  PEAKZ_FEDERATION_ID={v}")
    else:
        print("  PEAKZ_FEDERATION_ID  — not found (log in and try again)")

    if location_ids:
        for v in location_ids:
            print(f"  PEAKZ_LOCATION_ID={v}")
    else:
        print("  PEAKZ_LOCATION_ID    — not found (browse to booking calendar and try again)")

    print("\nThese values are already hardcoded as defaults in .env.test")
    print("Only update them if the values above differ from the defaults.")
    print("=" * 60)


def cmd_debug_slots() -> None:
    """
    Hit the slots API for the target booking date and pretty-print the raw
    response. Use this to diagnose parser issues without running a full booking.

    Usage:
      python main.py debug-slots
      BOOKING_DATE_OVERRIDE=2026-04-21 python main.py debug-slots
    """
    import json as _json
    from datetime import timedelta

    _setup_logging(debug=True)
    cfg = load_config()

    now_ams = _now_amsterdam()
    if cfg.booking_date_override:
        target_date = cfg.booking_date_override
    else:
        target_date = now_ams.date() + timedelta(weeks=cfg.booking_advance_weeks)

    print(f"\nTarget date: {target_date}")
    print(f"Location ID: {cfg.location_id}")
    print(f"Duration:    {cfg.duration_minutes} min\n")

    from padel_booking.foys_client import FoysClient
    with FoysClient(
        base_url=cfg.foys_base_url,
        email=cfg.peakz_email,
        password=cfg.peakz_password,
        federation_id=cfg.federation_id,
        location_id=cfg.location_id,
        reservation_type_id=cfg.reservation_type_id,
    ) as client:
        client._do_authenticate()

        iso_date = f"{target_date.isoformat()}T00:00:00"
        query = (
            f"reservationTypeId={cfg.reservation_type_id}"
            f"&locationId={cfg.location_id}"
            f"&playingTimes[]={cfg.duration_minutes}"
            f"&date={iso_date}"
        )
        path = f"/court-booking/public/api/v1/locations/search?{query}"
        token = client._ensure_token()
        resp = client._http.get(path, headers={"Authorization": f"Bearer {token}"})

        print(f"HTTP {resp.status_code}  GET {cfg.foys_base_url}{path}\n")
        print("=" * 60)
        try:
            print(_json.dumps(resp.json(), indent=2, ensure_ascii=False)[:4000])
        except Exception:
            print(resp.text[:4000])
        print("=" * 60)


def cmd_status() -> None:
    """Print the last 10 booking run records."""
    cfg = load_config()
    _setup_logging()
    from padel_booking.state import StateStore
    store = StateStore(cfg.state_db_path)
    runs = store.get_recent_runs(limit=10)
    if not runs:
        print("No booking runs recorded yet.")
        return
    print(f"\n{'ID':>4}  {'Trigger':>12}  {'Target':>12}  {'Status':>22}  {'Ref':>15}  {'Court':>20}")
    print("-" * 100)
    for r in runs:
        print(
            f"{r['id']:>4}  {r['trigger_date']:>12}  {r['target_booking_date']:>12}  "
            f"{r['status']:>22}  {str(r['reservation_reference'] or '-'):>15}  "
            f"{str(r['court_name'] or '-'):>20}"
        )
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "run-now": cmd_run_now,
    "schedule": cmd_schedule,
    "once-at": cmd_once_at,
    "discover": cmd_discover,
    "debug-slots": cmd_debug_slots,
    "status": cmd_status,
}


def main() -> None:
    _setup_logging()

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(__doc__)
        print("Available commands:", ", ".join(COMMANDS))
        sys.exit(0 if not cmd else 1)

    fn()


def _now_amsterdam():
    from datetime import datetime
    return datetime.now(TZ)


if __name__ == "__main__":
    main()
