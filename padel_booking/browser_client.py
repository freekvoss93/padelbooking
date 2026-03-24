"""
Playwright-based browser automation for Peakz Padel booking.

This module is the guaranteed fallback when the FOYS REST API endpoints
cannot be discovered or are unreliable.  It drives the real booking widget
at peakzpadel.nl exactly as a human would.

IMPORTANT — SELECTOR NOTES
===========================
Peakz Padel embeds the FOYS booking widget (a Vue.js SPA).  The selectors
below are based on FOYS widget conventions; they should be correct but may
need adjustment if FOYS updates their frontend.

To verify / fix selectors:
  1. Run:  python main.py discover   (opens a headed browser)
  2. Use DevTools to inspect elements during the booking flow
  3. Update the corresponding BROWSER_* env vars in .env
     (see config.py for the full list)
  4. Re-run in headed mode to confirm:  PLAYWRIGHT_HEADED=true python main.py run-now

All waits use generous timeouts so the code survives slow connections.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types (mirrors foys_client.py to keep the engine generic)
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    slot_id: str
    court_id: str
    court_name: str
    start_time: str
    end_time: str
    price: float
    court_type: str   # e.g. "baan", "outdoor" — from data attributes or name
    raw: dict


@dataclass
class BookingResult:
    reservation_reference: str
    court_id: str
    court_name: str
    start_time: str
    end_time: str
    total_price: float
    credits_used: bool
    raw: dict


class BrowserAuthError(Exception):
    """Login failed."""


class BrowserBookingError(Exception):
    """Booking step failed."""


# ---------------------------------------------------------------------------
# Browser client
# ---------------------------------------------------------------------------

class BrowserBookingClient:
    """
    Synchronous Playwright client.

    Usage:
        with BrowserBookingClient(config) as client:
            balance = client.get_credit_balance()
            slots   = client.find_available_slots(target_date, ["20:00","19:30","19:00"], 90)
            result  = client.book_slot(slots[0], dry_run=False)
    """

    # Default selector strategies — all overridable via env vars (see config.py)
    _SEL_LOGIN_LINK     = "text=Inloggen, a[href*='login'], button:has-text('Inloggen')"
    _SEL_EMAIL          = "input[type='email'], input[name='email'], input[name='username']"
    _SEL_PASSWORD       = "input[type='password']"
    _SEL_LOGIN_SUBMIT   = "button[type='submit'], input[type='submit']"
    _SEL_LOGGED_IN      = ".foys__avatar, .user-menu, [class*='account'], text=Mijn account"
    _SEL_DATE_CELL      = "td[data-date='{iso}'], button[data-date='{iso}'], [aria-label*='{human}']"
    _SEL_SLOT_TIME      = (
        ".timeslot:has-text('{time}'), "
        ".b-timeslot:has-text('{time}'), "
        "button.slot:has-text('{time}'), "
        "[class*='slot']:has-text('{time}')"
    )
    _SEL_CREDITS_RADIO  = (
        "input[value='credits'], input[value='tegoed'], "
        "label:has-text('Tegoed'), label:has-text('Credits'), "
        "[class*='payment']:has-text('Tegoed')"
    )
    _SEL_CONFIRM_BTN    = (
        "button:has-text('Bevestigen'), button:has-text('Confirm'), "
        "button:has-text('Reserveren'), button[type='submit']:visible"
    )
    _SEL_BOOKING_REF    = (
        ".confirmation-code, .reservation-reference, "
        "[class*='reference'], [class*='confirmation']"
    )
    _SEL_CREDIT_BALANCE = (
        ".credit-balance, .tegoed, [class*='wallet'], "
        "[class*='balance'], [class*='credits']"
    )

    def __init__(
        self,
        booking_url: str,
        email: str,
        password: str,
        screenshots_dir: str,
        headless: bool = True,
        slow_mo: int = 200,
    ) -> None:
        self.booking_url = booking_url
        self.email = email
        self.password = password
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo = slow_mo

        self._playwright = None
        self._browser = None
        self._page = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # lazy import
        self._playwright = sync_playwright().__enter__()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
        )
        self._page = context.new_page()
        self._page.set_default_timeout(30_000)
        return self

    def __exit__(self, *_):
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.__exit__(None, None, None)
        except Exception as exc:
            log.debug("Browser cleanup error: %s", exc)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Navigate to the booking page and log in if required."""
        log.info("Navigating to booking page: %s", self.booking_url)
        self._page.goto(self.booking_url, wait_until="networkidle")
        self._screenshot("01_booking_page_loaded")

        # Check if already logged in
        if self._is_logged_in():
            log.info("Already logged in")
            return

        # Find and click login link / button
        login_link = self._try_locator(self._SEL_LOGIN_LINK)
        if login_link:
            login_link.first.click()
            self._page.wait_for_load_state("networkidle")
            self._screenshot("02_login_page")
        else:
            log.debug("No explicit login link found — trying to fill credentials inline")

        # Fill credentials
        try:
            email_field = self._page.locator(self._SEL_EMAIL).first
            email_field.wait_for(state="visible", timeout=10_000)
            email_field.fill(self.email)

            pwd_field = self._page.locator(self._SEL_PASSWORD).first
            pwd_field.fill(self.password)
            self._screenshot("03_credentials_filled")

            submit = self._page.locator(self._SEL_LOGIN_SUBMIT).last
            submit.click()
            self._page.wait_for_load_state("networkidle")
            self._screenshot("04_after_login")
        except Exception as exc:
            self._screenshot("04_login_failed")
            self._save_html("login_failed")
            raise BrowserAuthError(f"Could not fill login form: {exc}") from exc

        if not self._is_logged_in():
            self._screenshot("05_login_check_failed")
            raise BrowserAuthError(
                "Login form submitted but no logged-in indicator found. "
                "Check selectors or credentials. Screenshot: 05_login_check_failed.png"
            )

        log.info("Login successful")
        # Return to booking page if we navigated away
        if self.booking_url not in self._page.url:
            self._page.goto(self.booking_url, wait_until="networkidle")

    def get_credit_balance(self) -> float:
        """Attempt to read the credit balance from the current page. Returns 0.0 if not found."""
        try:
            el = self._page.locator(self._SEL_CREDIT_BALANCE).first
            el.wait_for(state="visible", timeout=5_000)
            text = el.inner_text()
            return _parse_money(text)
        except Exception as exc:
            log.debug("Could not read credit balance from page: %s", exc)
            return 0.0

    def find_available_slots(
        self,
        target_date: date,
        preferred_times: list[str],
        duration_minutes: int,
        court_type: str = "baan",
    ) -> list[Slot]:
        """
        Navigate to the booking calendar for *target_date* and scrape
        available slots matching *preferred_times*.

        Returns slots in preference order (first match first).
        """
        self._navigate_to_date(target_date)
        self._screenshot(f"10_calendar_{target_date.isoformat()}")

        slots: list[Slot] = []
        for t in preferred_times:
            slot = self._find_slot_at_time(t, duration_minutes, target_date, court_type)
            if slot:
                slots.append(slot)

        log.info("Found %d matching slots for %s (court_type=%r): %s",
                 len(slots), target_date, court_type, [s.start_time for s in slots])
        return slots

    def book_slot(self, slot: Slot, dry_run: bool = False) -> BookingResult:
        """Click the slot, select credits as payment, and confirm."""
        if dry_run:
            log.info("DRY RUN — would book court=%r %s–%s", slot.court_name, slot.start_time, slot.end_time)
            return BookingResult(
                reservation_reference="DRY-RUN",
                court_id=slot.court_id,
                court_name=slot.court_name,
                start_time=slot.start_time,
                end_time=slot.end_time,
                total_price=slot.price,
                credits_used=True,
                raw={},
            )

        log.info("Booking slot: %s at %s", slot.court_name, slot.start_time)

        # Click the time slot element
        slot_el = self._page.locator(f"[data-slot-id='{slot.slot_id}']").first
        if not slot_el.is_visible():
            # Fallback: re-find by time text
            slot_el = self._page.locator(
                self._SEL_SLOT_TIME.format(time=slot.start_time)
            ).first
        try:
            slot_el.click()
            self._page.wait_for_load_state("domcontentloaded")
            self._screenshot("20_slot_selected")
        except Exception as exc:
            self._screenshot("20_slot_click_failed")
            raise BrowserBookingError(f"Could not click slot: {exc}") from exc

        # Select credits as payment method
        self._select_credits_payment()
        self._screenshot("21_payment_selected")

        # Confirm booking
        try:
            confirm = self._page.locator(self._SEL_CONFIRM_BTN).last
            confirm.wait_for(state="visible", timeout=10_000)
            confirm.click()
            self._page.wait_for_load_state("networkidle")
            self._screenshot("22_after_confirm")
        except Exception as exc:
            self._screenshot("22_confirm_failed")
            self._save_html("confirm_failed")
            raise BrowserBookingError(f"Confirm button error: {exc}") from exc

        # Extract reservation reference
        ref = self._extract_reservation_reference()
        if not ref:
            self._screenshot("23_no_reference_found")
            log.warning("Booking completed but no reservation reference found on page")
            ref = "UNKNOWN"

        log.info("Booking confirmed — reference: %s", ref)
        return BookingResult(
            reservation_reference=ref,
            court_id=slot.court_id,
            court_name=slot.court_name,
            start_time=slot.start_time,
            end_time=slot.end_time,
            total_price=slot.price,
            credits_used=True,
            raw={"page_url": self._page.url},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_logged_in(self) -> bool:
        try:
            self._page.locator(self._SEL_LOGGED_IN).wait_for(
                state="visible", timeout=3_000
            )
            return True
        except Exception:
            return False

    def _navigate_to_date(self, target_date: date) -> None:
        """
        Navigate the FOYS calendar widget to *target_date*.

        Tries two strategies:
         A) Click on a date cell with data-date attribute matching the ISO date.
         B) Click next-month arrows until the correct month is visible.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        iso = target_date.isoformat()
        human = _dutch_date(target_date)

        # Strategy A: direct data-date click
        sel_a = self._SEL_DATE_CELL.format(iso=iso, human=human)
        try:
            cell = self._page.locator(sel_a).first
            cell.wait_for(state="visible", timeout=5_000)
            cell.click()
            log.debug("Calendar: clicked date cell directly (%s)", iso)
            self._page.wait_for_load_state("domcontentloaded")
            return
        except PWTimeout:
            log.debug("Direct date cell not visible — trying month navigation")

        # Strategy B: navigate months
        current_month = _get_visible_calendar_month(self._page)
        max_clicks = 6
        for _ in range(max_clicks):
            if current_month is None or current_month >= target_date.replace(day=1):
                break
            next_btn = self._page.locator(
                "button[aria-label*='next'], button[aria-label*='volgende'], "
                ".calendar-next, [class*='next-month']"
            ).first
            if next_btn.is_visible():
                next_btn.click()
                self._page.wait_for_timeout(500)
            current_month = _get_visible_calendar_month(self._page)

        # Try the date cell again
        try:
            cell = self._page.locator(sel_a).first
            cell.wait_for(state="visible", timeout=5_000)
            cell.click()
            self._page.wait_for_load_state("domcontentloaded")
            log.debug("Calendar: clicked date after month navigation (%s)", iso)
        except PWTimeout as exc:
            self._screenshot("10_calendar_navigation_failed")
            raise BrowserBookingError(
                f"Could not navigate calendar to {iso}. "
                "The widget may use a different date attribute. "
                "Run with PLAYWRIGHT_HEADED=true to inspect."
            ) from exc

    def _find_slot_at_time(
        self, time_str: str, duration_minutes: int, target_date: date,
        court_type: str = "baan",
    ) -> Optional[Slot]:
        """Return a Slot if *time_str* is available, enabled, and matches court_type."""
        sel = self._SEL_SLOT_TIME.format(time=time_str)
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            el = self._page.locator(sel).first
            el.wait_for(state="visible", timeout=3_000)

            # Check if the slot is bookable (not greyed out / disabled)
            cls = (el.get_attribute("class") or "").lower()
            disabled = el.get_attribute("disabled")
            if disabled is not None or any(
                s in cls for s in ("disabled", "unavailable", "full", "gereserveerd")
            ):
                log.debug("Slot %s exists but is not available", time_str)
                return None

            # Read price if visible
            price = _parse_money(el.inner_text())

            # Read slot_id / court info from data attributes
            slot_id = el.get_attribute("data-slot-id") or el.get_attribute("data-id") or time_str
            court_id = el.get_attribute("data-court-id") or "1"
            court_name = el.get_attribute("data-court-name") or "Court"
            # Court type from data attribute, or fall back to "baan"
            detected_type = (
                el.get_attribute("data-court-type")
                or el.get_attribute("data-type")
                or _infer_court_type_from_name(court_name)
            )

            # Filter: skip if court type does not match the configured type
            if not _court_type_matches(detected_type, court_type):
                log.debug(
                    "Slot %s skipped: court_type %r does not match required %r",
                    time_str, detected_type, court_type,
                )
                return None

            end_h = int(time_str[:2]) * 60 + int(time_str[3:5]) + duration_minutes
            end_time = f"{end_h // 60:02d}:{end_h % 60:02d}"

            log.info("Found available slot: %s court=%s type=%s", time_str, court_name, detected_type)
            return Slot(
                slot_id=slot_id,
                court_id=court_id,
                court_name=court_name,
                start_time=time_str,
                end_time=end_time,
                price=price,
                court_type=detected_type,
                raw={},
            )
        except Exception as exc:
            log.debug("Slot %s not found or not available: %s", time_str, exc)
            return None

    def _select_credits_payment(self) -> None:
        """Select the credits / tegoed payment option."""
        from playwright.sync_api import TimeoutError as PWTimeout
        try:
            el = self._page.locator(self._SEL_CREDITS_RADIO).first
            el.wait_for(state="visible", timeout=8_000)
            # If it's a radio/checkbox, click it; if it's a label, click it directly
            tag = (el.evaluate("el => el.tagName") or "").lower()
            if tag == "input":
                el.check()
            else:
                el.click()
            log.debug("Credits payment method selected")
        except PWTimeout:
            self._screenshot("21_payment_credits_not_found")
            raise BrowserBookingError(
                "Could not find the credits/tegoed payment option. "
                "The account may have no credits, or the selector needs updating."
            )

    def _extract_reservation_reference(self) -> Optional[str]:
        """Try to read the reservation reference from the confirmation page."""
        try:
            el = self._page.locator(self._SEL_BOOKING_REF).first
            el.wait_for(state="visible", timeout=5_000)
            text = el.inner_text()
            # Extract alphanumeric reference
            m = re.search(r"[A-Z0-9]{6,}", text)
            return m.group(0) if m else text.strip()
        except Exception:
            # Try to extract from the page URL (some systems put ?ref=... in URL)
            url = self._page.url
            m = re.search(r"[?&]ref=([^&]+)", url)
            return m.group(1) if m else None

    def _try_locator(self, selector: str):
        """Return a locator if visible, else None."""
        try:
            loc = self._page.locator(selector).first
            loc.wait_for(state="visible", timeout=2_000)
            return loc
        except Exception:
            return None

    def _screenshot(self, name: str) -> None:
        path = self.screenshots_dir / f"{name}.png"
        try:
            self._page.screenshot(path=str(path))
            log.debug("Screenshot: %s", path)
        except Exception as exc:
            log.debug("Screenshot failed (%s): %s", name, exc)

    def _save_html(self, name: str) -> None:
        path = self.screenshots_dir / f"{name}.html"
        try:
            path.write_text(self._page.content(), encoding="utf-8")
            log.debug("HTML saved: %s", path)
        except Exception as exc:
            log.debug("HTML save failed (%s): %s", name, exc)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _infer_court_type_from_name(name: str) -> str:
    """Guess court type from its display name."""
    n = name.lower()
    if "outdoor" in n or "buiten" in n:
        return "outdoor"
    if "single" in n or "enkel" in n:
        return "single"
    return "baan"   # default: indoor double


def _court_type_matches(detected: str, required: str) -> bool:
    """
    Return True if *detected* satisfies the *required* court type filter.

    The required type "baan" accepts anything that isn't explicitly outdoor
    or single, so it works even when the API returns no type at all.
    Matching is case-insensitive and partial (substring).
    """
    if not required:
        return True
    d = (detected or "").lower()
    r = required.lower()
    if r == "baan":
        # Accept: "baan", "indoor", "", unknown — reject: "outdoor", "single"
        return "outdoor" not in d and "buiten" not in d and "single" not in d and "enkel" not in d
    return r in d or d in r


def _parse_money(text: str) -> float:
    """Extract a numeric EUR amount from text like '€ 12,50' or '12.50'."""
    m = re.search(r"[\d]+[,.][\d]{2}", text)
    if m:
        return float(m.group(0).replace(",", "."))
    m = re.search(r"\d+", text)
    return float(m.group(0)) if m else 0.0


def _dutch_date(d: date) -> str:
    """Return a Dutch human-readable date string for ARIA label matching."""
    months = [
        "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ]
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _get_visible_calendar_month(page) -> Optional[date]:
    """Try to read the displayed month from common calendar header patterns."""
    try:
        text = page.locator(
            ".calendar-header, .fc-toolbar-title, [class*='calendar-title'], "
            "[class*='month-label']"
        ).first.inner_text()
        # e.g. "april 2026"
        months = {
            "januari": 1, "februari": 2, "maart": 3, "april": 4,
            "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
            "september": 9, "oktober": 10, "november": 11, "december": 12,
        }
        for name, num in months.items():
            if name in text.lower():
                m = re.search(r"\d{4}", text)
                year = int(m.group(0)) if m else date.today().year
                return date(year, num, 1)
    except Exception:
        pass
    return None
