"""
FOYS REST API client for Peakz Padel.

==========================================================================
ALL ENDPOINTS CONFIRMED from live browser traffic (2026-03-24)
==========================================================================
Auth:
  POST /foys/api/v1/token
    Content-Type: application/x-www-form-urlencoded
    Body: grant_type=password&username=…&password=…&federationId=UUID

Slot availability (public — no auth required):
  GET /court-booking/public/api/v1/locations/search
    ?reservationTypeId=6
    &locationId=UUID
    &playingTimes[]=90
    &date=2026-04-21T00:00:00

Court inventory (physical courts list):
  GET /court-booking/public/api/v1/inventory-items
    ?locationId=UUID&reservationTypeId=6&date=2026-04-21T00:00:00

Credit balance:
  GET /finance/members-api/Members/account        (needs auth)

Create booking:
  POST /court-booking/members/api/v1/bookings     (needs auth)

Known IDs for Peakz Padel Nijmegen Westerpark:
  federationId = df82f4dd-fd87-4af5-9c2f-656fe1a44357
  locationId   = b575a17a-a618-4de7-9127-51a15bff75f1
  reservationTypeId = 6
==========================================================================
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def _transient_retry(func):
    """Wrap *func* with exponential-backoff retry on transient network errors."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )(func)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    slot_id: str        # inventoryItemId or composite key used in booking POST
    court_id: str
    court_name: str
    start_time: str     # "HH:MM"
    end_time: str       # "HH:MM"
    price: float
    court_type: str     # "baan", "outdoor", etc.
    raw: dict           # original response item for debugging


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


class FoysAuthError(Exception):
    """Wrong credentials or token expired."""


class FoysBookingError(Exception):
    """Backend rejected the booking."""


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

@dataclass
class _Token:
    access: str
    refresh: str
    expires_at: float   # unix timestamp


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class FoysClient:
    """
    Synchronous FOYS API client.

    Token management is automatic: re-authenticates when the access token
    is within 60 s of expiry.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        federation_id: str,
        location_id: str,
        reservation_type_id: int = 6,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.federation_id = federation_id
        self.location_id = location_id
        self.reservation_type_id = reservation_type_id

        self._token: Optional[_Token] = None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_available_slots(self, target_date: date, duration_minutes: int) -> list[Slot]:
        """
        Return available slots for *target_date* at the configured location.

        Uses the public /locations/search endpoint — no auth required for reading,
        but we send the Bearer token anyway so the API can return member-specific
        pricing if applicable.
        """
        iso_date = f"{target_date.isoformat()}T00:00:00"

        # Keep brackets unencoded — the FOYS API uses playingTimes[]=90 literally.
        # urlencode() would produce playingTimes%5B%5D=90 which the API rejects.
        query = (
            f"reservationTypeId={self.reservation_type_id}"
            f"&locationId={self.location_id}"
            f"&playingTimes[]={duration_minutes}"
            f"&date={iso_date}"
        )
        path = f"/court-booking/public/api/v1/locations/search?{query}"

        try:
            token = self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
        except Exception:
            headers = {}   # public endpoint — proceed without auth if login fails

        log.info("GET %s%s", self.base_url, path)
        resp = self._http.get(path, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        # Always log the raw response so parse failures are diagnosable
        log.info("locations/search raw response: %s", str(data)[:2000])

        slots = _parse_slots(data, duration_minutes)
        if not slots:
            log.warning(
                "Parsed 0 slots — response did not match any known shape. "
                "Full response: %s", data
            )
        return slots

    def get_credit_balance(self) -> float:
        """Return prepaid credit balance in EUR. Returns 0.0 on any failure."""
        try:
            token = self._ensure_token()
            resp = self._http.get(
                "/finance/members-api/Members/account",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            log.debug("Members/account raw: %s", str(data)[:300])
            return _parse_balance(data)
        except Exception as exc:
            log.warning("Could not fetch credit balance: %s", exc)
            return 0.0

    def create_booking(
        self,
        slot: Slot,
        use_credits: bool = True,
        dry_run: bool = False,
    ) -> BookingResult:
        """Create a booking for *slot*, paying with credits."""
        if dry_run:
            log.info(
                "DRY RUN — would book court=%r %s–%s price=%.2f",
                slot.court_name, slot.start_time, slot.end_time, slot.price,
            )
            return BookingResult(
                reservation_reference="DRY-RUN",
                court_id=slot.court_id,
                court_name=slot.court_name,
                start_time=slot.start_time,
                end_time=slot.end_time,
                total_price=slot.price,
                credits_used=use_credits,
                raw={},
            )

        token = self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # ------------------------------------------------------------------
        # Step 1: Create booking session
        # Confirmed payload shape from live browser traffic (2026-03-24):
        #   startDateTime / endDateTime without seconds, e.g. "2026-04-21T18:30"
        #   reservations is an array: [{"inventoryItemId": 965}]
        # ------------------------------------------------------------------
        start_raw = slot.raw.get("startTime", "")   # "2026-04-21T18:30:00"
        end_raw   = slot.raw.get("endTime",   "")   # "2026-04-21T20:00:00"
        start_dt  = start_raw[:16] if len(start_raw) >= 16 else start_raw
        end_dt    = end_raw[:16]   if len(end_raw)   >= 16 else end_raw

        step1: dict[str, Any] = {
            "reservationTypeId": self.reservation_type_id,
            "startDateTime": start_dt,
            "endDateTime": end_dt,
            "reservations": [{"inventoryItemId": int(slot.court_id)}],
        }
        log.info("Step 1 — create booking session: %s", step1)
        r1 = self._http.post("/court-booking/members/api/v1/bookings", json=step1, headers=headers)
        log.info("Step 1 response: HTTP %d — %s", r1.status_code, r1.text[:600])
        if not r1.is_success:
            raise FoysBookingError(f"Step 1 failed (HTTP {r1.status_code}): {r1.text[:400]}")
        session = r1.json()
        booking_guid = session.get("guid") or session.get("id")
        if not booking_guid:
            raise FoysBookingError(f"Step 1: no guid in response: {r1.text[:300]}")
        log.info("Booking session guid=%s (timeOut=%s)", booking_guid, session.get("timeOutAt"))

        # ------------------------------------------------------------------
        # Step 2: Pay with credits
        # Confirmed endpoint from live browser traffic: POST /pay/credits
        # ------------------------------------------------------------------
        log.info("Step 2 — pay with credits (guid=%s)", booking_guid)
        r2 = self._http.post(
            f"/court-booking/members/api/v1/bookings/{booking_guid}/pay/credits",
            json={},
            headers=headers,
        )
        log.info("Step 2 response: HTTP %d — %s", r2.status_code, r2.text[:600])
        if not r2.is_success:
            raise FoysBookingError(f"Step 2 (pay/credits) failed (HTTP {r2.status_code}): {r2.text[:400]}")

        # pay/credits returns HTTP 200 with empty body on success
        data = r2.json() if r2.content else session
        return _parse_booking_result(data, slot, use_credits)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token.expires_at - 60:
            return self._token.access

        if self._token and self._token.refresh:
            try:
                self._do_refresh()
                return self._token.access  # type: ignore[union-attr]
            except Exception as exc:
                log.debug("Refresh failed (%s) — re-authenticating", exc)

        self._do_authenticate()
        return self._token.access  # type: ignore[union-attr]

    @_transient_retry
    def _do_authenticate(self) -> None:
        log.info("Authenticating with FOYS (federationId=%s)", self.federation_id)
        # FOYS uses OAuth2 password grant with form-encoded body
        resp = self._http.post(
            "/foys/api/v1/token",
            content=urlencode({
                "grant_type": "password",
                "username": self.email,
                "password": self.password,
                "federationId": self.federation_id,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code in (400, 401, 403):
            raise FoysAuthError(
                f"Authentication failed (HTTP {resp.status_code}). "
                "Check PEAKZ_EMAIL, PEAKZ_PASSWORD, and PEAKZ_FEDERATION_ID."
            )
        resp.raise_for_status()
        self._store_token(resp.json())
        log.info("Authenticated — token valid ~%ds", resp.json().get("expires_in", 3600))

    def _do_refresh(self) -> None:
        resp = self._http.post(
            "/foys/api/v1/token",
            content=urlencode({
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh,  # type: ignore[union-attr]
                "federationId": self.federation_id,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        self._store_token(resp.json())
        log.debug("Token refreshed")

    def _store_token(self, data: dict) -> None:
        self._token = _Token(
            access=data["access_token"],
            refresh=data.get("refresh_token", ""),
            expires_at=time.time() + int(data.get("expires_in", 3600)),
        )


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def _parse_slots(data: Any, duration_minutes: int) -> list[Slot]:
    """
    Parse the /locations/search response into Slot objects.

    The response shape from the captured traffic appears to be a list of
    location objects, each containing inventory items with available timeslots.
    We handle both that shape and simpler flat arrays.
    """
    """
    Confirmed response shape (2026-03-24):

    [                                        ← list of location objects
      {
        "inventoryItemsTimeSlots": [         ← list of courts
          {
            "id": 958,                       ← integer court ID
            "name": "Baan 1",
            "type": "Double court indoor",
            "timeSlots": [                   ← available timeslots
              {
                "startTime": "2026-04-21T20:00:00",
                "endTime":   "2026-04-21T21:30:00",
                "price": 57.0,
                "isAvailable": true,
                "duration": 90
              }
            ]
          }
        ]
      }
    ]
    """
    slots: list[Slot] = []

    # Unwrap a possible dict envelope (future-proofing)
    if isinstance(data, dict):
        for key in ("locations", "data", "results", "items"):
            if key in data:
                data = data[key]
                break

    if not isinstance(data, list):
        log.warning("Unexpected slots response type %s", type(data))
        return slots

    for location in data:
        if not isinstance(location, dict):
            continue
        for item in location.get("inventoryItemsTimeSlots") or []:
            court_id = str(item.get("id", ""))
            court_name = item.get("name") or "Court"
            court_type = (item.get("type") or "baan").lower()  # "double court indoor"

            for ts in item.get("timeSlots") or []:
                if not ts.get("isAvailable", False):
                    continue   # skip booked/unavailable slots
                start = _fmt_time(ts.get("startTime", ""))
                end   = _fmt_time(ts.get("endTime", ""))
                if not start:
                    continue
                slots.append(Slot(
                    slot_id=f"{court_id}_{start}",
                    court_id=court_id,
                    court_name=court_name,
                    start_time=start,
                    end_time=end,
                    price=float(ts.get("price") or 0.0),
                    court_type=court_type,
                    raw=ts,   # includes "startTime" ISO string needed for booking POST
                ))

    log.info("Parsed %d available slots from API", len(slots))
    return slots


def _parse_balance(data: Any) -> float:
    """Extract EUR credit balance from Members/account response."""
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        # Common field names in FOYS finance responses
        for key in ("balance", "credits", "creditBalance", "walletBalance",
                    "prepaidBalance", "amount", "wallet"):
            val = data.get(key)
            if val is not None:
                if isinstance(val, (int, float)):
                    return float(val)
                if isinstance(val, dict):
                    return _parse_balance(val)
        # Nested: {"account": {"balance": 50.0}}
        for nested in ("account", "data", "wallet", "finance"):
            if nested in data:
                return _parse_balance(data[nested])
    log.debug("Could not extract balance from: %s", str(data)[:200])
    return 0.0


def _parse_booking_result(data: dict, slot: Slot, credits_used: bool) -> BookingResult:
    # Reservation reference: prefer reservations[0].guid (confirmed response shape)
    reservation_ref = "unknown"
    for res in data.get("reservations") or []:
        reservation_ref = str(res.get("guid") or res.get("id") or "unknown")
        break
    if reservation_ref == "unknown":
        reservation_ref = str(
            data.get("id")
            or data.get("bookingId")
            or data.get("reservationId")
            or data.get("reference")
            or data.get("guid")
            or "unknown"
        )
    return BookingResult(
        reservation_reference=reservation_ref,
        court_id=slot.court_id,
        court_name=slot.court_name,
        start_time=slot.start_time,
        end_time=slot.end_time,
        total_price=float(data.get("totalPrice") or data.get("amount") or slot.price),
        credits_used=credits_used,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_court_type(item: dict) -> str:
    for key in ("type", "courtType", "kind", "category", "baanType"):
        val = item.get(key)
        if val:
            return str(val).lower()
    name = (item.get("name") or item.get("courtName") or "").lower()
    if "outdoor" in name or "buiten" in name:
        return "outdoor"
    if "single" in name or "enkel" in name:
        return "single"
    return "baan"


def _fmt_time(raw: str) -> str:
    """Normalise any time representation to HH:MM."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        pass
    return str(raw)[:5]


def _hhmm_to_mins(t: str) -> int:
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except Exception:
        return 0


def _mins_to_hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _duration_minutes(start: str, end: str) -> int:
    d = _hhmm_to_mins(end) - _hhmm_to_mins(start)
    return d if d > 0 else 90
