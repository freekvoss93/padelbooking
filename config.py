"""
Configuration — loaded exclusively from environment variables.
Call load_config() after python-dotenv has loaded the .env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pytz

TZ = pytz.timezone("Europe/Amsterdam")


@dataclass
class Config:
    # --- Credentials --------------------------------------------------------
    peakz_email: str
    peakz_password: str

    # --- Club ---------------------------------------------------------------
    federation_id: str   # Peakz federation UUID — used in OAuth token request
    location_id: str     # Specific club/location UUID — used in slot search
    reservation_type_id: int
    club_name: str

    # --- Booking preferences ------------------------------------------------
    preferred_times: list[str]
    duration_minutes: int
    booking_advance_weeks: int
    court_type: str          # e.g. "baan" — only book courts of this type

    # --- Credits ------------------------------------------------------------
    min_credit_balance: float

    # --- Notifications -------------------------------------------------------
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    notification_to: str

    # --- Modes ---------------------------------------------------------------
    test_mode: bool
    force_run_now: bool
    dry_run: bool
    test_run_at: Optional[datetime]
    booking_date_override: Optional[date]

    # --- Backend ------------------------------------------------------------
    use_api: bool
    foys_base_url: str
    peakz_booking_url: str

    # --- Storage ------------------------------------------------------------
    state_db_path: str
    screenshots_dir: str


def load_config() -> Config:
    def _s(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    def _b(key: str, default: bool = False) -> bool:
        val = os.environ.get(key, "").strip().lower()
        if not val:
            return default
        return val in ("1", "true", "yes")

    def _f(key: str, default: float) -> float:
        val = os.environ.get(key, "").strip()
        return float(val) if val else default

    def _i(key: str, default: int) -> int:
        val = os.environ.get(key, "").strip()
        return int(val) if val else default

    def _opt(key: str) -> Optional[str]:
        val = os.environ.get(key, "").strip()
        return val if val else None

    # Parse TEST_RUN_AT — treat naive datetimes as Amsterdam-local
    test_run_at: Optional[datetime] = None
    if raw := _s("TEST_RUN_AT"):
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = TZ.localize(dt)
        test_run_at = dt

    # Parse BOOKING_DATE_OVERRIDE
    booking_date_override: Optional[date] = None
    if raw := _s("BOOKING_DATE_OVERRIDE"):
        booking_date_override = date.fromisoformat(raw)

    times_raw = _s("PREFERRED_TIMES", "20:00,19:30,19:00,18:30,20:30")
    preferred_times = [t.strip() for t in times_raw.split(",") if t.strip()]

    return Config(
        peakz_email=_s("PEAKZ_EMAIL"),
        peakz_password=_s("PEAKZ_PASSWORD"),
        federation_id=_s("PEAKZ_FEDERATION_ID", "df82f4dd-fd87-4af5-9c2f-656fe1a44357"),
        location_id=_s("PEAKZ_LOCATION_ID", "b575a17a-a618-4de7-9127-51a15bff75f1"),
        reservation_type_id=_i("PEAKZ_RESERVATION_TYPE_ID", 6),
        club_name=_s("PEAKZ_CLUB_NAME", "Peakz Padel Nijmegen Westerpark"),
        preferred_times=preferred_times,
        duration_minutes=_i("DURATION_MINUTES", 90),
        booking_advance_weeks=_i("BOOKING_ADVANCE_WEEKS", 4),
        court_type=_s("COURT_TYPE", "baan"),
        min_credit_balance=_f("MIN_CREDIT_BALANCE", 20.0),
        smtp_host=_s("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_i("SMTP_PORT", 587),
        smtp_user=_s("SMTP_USER"),
        smtp_password=_s("SMTP_PASSWORD"),
        smtp_from=_s("SMTP_FROM"),
        notification_to=_s("NOTIFICATION_TO"),
        test_mode=_b("TEST_MODE"),
        force_run_now=_b("FORCE_RUN_NOW"),
        dry_run=_b("DRY_RUN"),
        test_run_at=test_run_at,
        booking_date_override=booking_date_override,
        use_api=_b("USE_API", True),
        foys_base_url=_s("FOYS_BASE_URL", "https://api.foys.io"),
        peakz_booking_url=_s("PEAKZ_BOOKING_URL", "https://peakzpadel.nl/reserveren"),
        state_db_path=_s("STATE_DB_PATH", "data/state.db"),
        screenshots_dir=_s("SCREENSHOTS_DIR", "data/screenshots"),
    )
