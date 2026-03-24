"""
Booking engine — the single source of truth for the weekly booking flow.

This module is intentionally backend-agnostic: it accepts either a
FoysClient (REST API) or a BrowserBookingClient (Playwright) through duck
typing, so the test path and the production path use the exact same logic.

Flow
----
1. Check idempotency (already booked this target date? skip.)
2. Authenticate / open browser session
3. Read credit balance
4. Bail early if credits < min_credit_balance
5. Try preferred times in order; take the first available slot
6. Book the slot (skipped in dry-run mode)
7. Read updated credit balance
8. Persist result to state DB
9. Send notification
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Union

import pytz

from config import Config, TZ
from padel_booking.notifier import NotificationContext, Notifier
from padel_booking.state import BookingRun, StateStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    status: str                      # matches BookingRun.status
    run_id: Optional[int] = None
    reservation_reference: Optional[str] = None
    court_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    total_price: Optional[float] = None
    credit_balance_before: Optional[float] = None
    credit_balance_after: Optional[float] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BookingEngine:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        notifier: Notifier,
    ) -> None:
        self.cfg = config
        self.state = state
        self.notifier = notifier

    def run(
        self,
        trigger_date: date,
        target_date: Optional[date] = None,
    ) -> EngineResult:
        """
        Execute the full booking workflow.

        Parameters
        ----------
        trigger_date : The date on which this run was triggered (used as the
                       idempotency key and for the state record).
        target_date  : The date to book a court for.  Defaults to
                       trigger_date + booking_advance_weeks * 7 days.
        """
        if target_date is None:
            target_date = trigger_date + timedelta(weeks=self.cfg.booking_advance_weeks)

        log = logging.LoggerAdapter(
            logging.getLogger(__name__),
            {"trigger": str(trigger_date), "target": str(target_date)},
        )

        log.info(
            "Engine.run: trigger=%s target=%s dry_run=%s",
            trigger_date, target_date, self.cfg.dry_run,
        )

        # ----------------------------------------------------------------
        # 1. Idempotency check
        # ----------------------------------------------------------------
        if not self.cfg.force_run_now and not self.cfg.dry_run:
            if self.state.has_successful_booking(target_date):
                log.info("Target date %s already has a confirmed booking — skipping", target_date)
                return EngineResult(status="already_booked")

        # ----------------------------------------------------------------
        # 2. Create the run record as "pending"
        # ----------------------------------------------------------------
        run = BookingRun(
            trigger_date=trigger_date,
            target_booking_date=target_date,
            club_name=self.cfg.club_name,
            status="pending",
        )
        run_id = self.state.create_run(run)
        log.info("State record created: run_id=%d", run_id)

        try:
            return self._execute(trigger_date, target_date, run_id, log)
        except Exception as exc:
            err = str(exc)
            log.error("Unexpected engine error: %s", err, exc_info=True)
            self.state.update_run(run_id, status="error", error_message=err[:1000])
            self._notify(
                "error",
                trigger_date=trigger_date,
                target_date=target_date,
                error_message=err[:500],
                run_id=run_id,
            )
            return EngineResult(status="error", run_id=run_id, error_message=err)

    # ------------------------------------------------------------------
    # Core flow (separated so exceptions always reach the outer handler)
    # ------------------------------------------------------------------

    def _execute(
        self,
        trigger_date: date,
        target_date: date,
        run_id: int,
        log,
    ) -> EngineResult:
        client = self._build_client()
        is_browser = hasattr(client, "login")

        if is_browser:
            log.info("Using Playwright browser client")

        # Open the client and guarantee cleanup in a single try/finally block
        try:
            if hasattr(client, "__enter__"):
                client.__enter__()

            # ----------------------------------------------------------------
            # 3. Authenticate / open browser session
            # ----------------------------------------------------------------
            if is_browser:
                client.login()

            return self._execute_with_client(
                client, trigger_date, target_date, run_id, log
            )
        finally:
            try:
                if hasattr(client, "__exit__"):
                    client.__exit__(None, None, None)
                elif hasattr(client, "close"):
                    client.close()
            except Exception as exc:
                log.debug("Client cleanup error (ignored): %s", exc)

    def _execute_with_client(
        self, client, trigger_date, target_date, run_id, log
    ) -> EngineResult:

        # ----------------------------------------------------------------
        # 4. Credit balance check
        # ----------------------------------------------------------------
        balance_before = client.get_credit_balance()
        self.state.update_run(run_id, credit_balance_before=balance_before)
        log.info("Credit balance before booking: €%.2f", balance_before)

        if balance_before < self.cfg.min_credit_balance:
            log.warning(
                "Insufficient credits (€%.2f < €%.2f) — skipping booking",
                balance_before, self.cfg.min_credit_balance,
            )
            self.state.update_run(run_id, status="insufficient_credits")
            self._notify(
                "insufficient_credits",
                trigger_date=trigger_date,
                target_date=target_date,
                credit_balance=balance_before,
                run_id=run_id,
            )
            return EngineResult(
                status="insufficient_credits",
                run_id=run_id,
                credit_balance_before=balance_before,
            )

        # ----------------------------------------------------------------
        # 5. Find available slot
        # ----------------------------------------------------------------
        if hasattr(client, "find_available_slots"):
            # Playwright client — passes court_type filter directly
            slots = client.find_available_slots(
                target_date,
                self.cfg.preferred_times,
                self.cfg.duration_minutes,
                court_type=self.cfg.court_type,
            )
        else:
            # REST API client — fetch all slots then filter here
            slots = client.get_available_slots(target_date, self.cfg.duration_minutes)

        # Filter by court type and pick first matching preferred time
        chosen = _pick_preferred_slot(slots, self.cfg.preferred_times, self.cfg.court_type)

        if chosen is None:
            log.warning("No slots available for %s at preferred times", target_date)
            self.state.update_run(run_id, status="no_slots")
            self._notify(
                "no_slots",
                trigger_date=trigger_date,
                target_date=target_date,
                run_id=run_id,
            )
            return EngineResult(status="no_slots", run_id=run_id)

        log.info(
            "Selected slot: court=%s %s–%s price=€%.2f",
            chosen.court_name, chosen.start_time, chosen.end_time, chosen.price,
        )
        self.state.update_run(
            run_id,
            court_id=chosen.court_id,
            court_name=chosen.court_name,
            start_time=chosen.start_time,
            end_time=chosen.end_time,
            total_price=chosen.price,
        )

        # ----------------------------------------------------------------
        # 6. Book the slot
        # ----------------------------------------------------------------
        book_kwargs = dict(dry_run=self.cfg.dry_run)
        if hasattr(client, "book_slot"):
            result = client.book_slot(chosen, **book_kwargs)
        else:
            result = client.create_booking(chosen, use_credits=True, **book_kwargs)

        # ----------------------------------------------------------------
        # 7. Read updated balance
        # ----------------------------------------------------------------
        if self.cfg.dry_run:
            # No real payment was made — compute expected balance
            balance_after = round(balance_before - (result.total_price or 0.0), 2)
        else:
            balance_after = client.get_credit_balance()
        log.info("Credit balance after booking: €%.2f", balance_after)

        # ----------------------------------------------------------------
        # 8. Persist result
        # ----------------------------------------------------------------
        final_status = "success" if not self.cfg.dry_run else "dry_run_success"
        self.state.update_run(
            run_id,
            status=final_status,
            booking_status="confirmed",
            payment_status="paid_credits",
            credits_used=True,
            credit_balance_after=balance_after,
            reservation_reference=result.reservation_reference,
            total_price=result.total_price,
        )

        log.info(
            "Booking %s — ref=%s court=%s %s–%s",
            final_status, result.reservation_reference,
            result.court_name, result.start_time, result.end_time,
        )

        # ----------------------------------------------------------------
        # 9. Notify
        # ----------------------------------------------------------------
        self._notify(
            "booking_success",
            trigger_date=trigger_date,
            target_date=target_date,
            start_time=result.start_time,
            end_time=result.end_time,
            court_name=result.court_name,
            reservation_reference=result.reservation_reference,
            total_price=result.total_price,
            credit_balance=balance_after,
            run_id=run_id,
        )

        return EngineResult(
            status=final_status,
            run_id=run_id,
            reservation_reference=result.reservation_reference,
            court_name=result.court_name,
            start_time=result.start_time,
            end_time=result.end_time,
            total_price=result.total_price,
            credit_balance_before=balance_before,
            credit_balance_after=balance_after,
        )

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _build_client(self):
        if self.cfg.use_api and self.cfg.federation_id:
            from padel_booking.foys_client import FoysClient
            log.info("Building FOYS REST API client")
            return FoysClient(
                base_url=self.cfg.foys_base_url,
                email=self.cfg.peakz_email,
                password=self.cfg.peakz_password,
                federation_id=self.cfg.federation_id,
                location_id=self.cfg.location_id,
                reservation_type_id=self.cfg.reservation_type_id,
            )
        else:
            from padel_booking.browser_client import BrowserBookingClient
            import os
            headless = os.environ.get("PLAYWRIGHT_HEADED", "").lower() not in ("1", "true")
            log.info("Building Playwright browser client (headless=%s)", headless)
            return BrowserBookingClient(
                booking_url=self.cfg.peakz_booking_url,
                email=self.cfg.peakz_email,
                password=self.cfg.peakz_password,
                screenshots_dir=self.cfg.screenshots_dir,
                headless=headless,
            )

    # ------------------------------------------------------------------
    # Notification helper
    # ------------------------------------------------------------------

    def _notify(self, event: str, trigger_date: date, target_date: date, **kwargs) -> None:
        ctx = NotificationContext(
            event=event,
            trigger_date=str(trigger_date),
            target_date=str(target_date),
            club_name=self.cfg.club_name,
            dry_run=self.cfg.dry_run,
            start_time=kwargs.get("start_time"),
            end_time=kwargs.get("end_time"),
            court_name=kwargs.get("court_name"),
            reservation_reference=kwargs.get("reservation_reference"),
            total_price=kwargs.get("total_price"),
            credit_balance=kwargs.get("credit_balance"),
            error_message=kwargs.get("error_message"),
        )
        self.notifier.send(ctx)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_preferred_slot(slots, preferred_times: list[str], court_type: str = ""):
    """
    Return the first slot whose start_time matches a preferred time (in order)
    and whose court_type satisfies the filter.
    """
    from padel_booking.browser_client import _court_type_matches

    eligible = [s for s in slots if _court_type_matches(getattr(s, "court_type", ""), court_type)]
    by_time = {s.start_time: s for s in eligible}
    for t in preferred_times:
        if t in by_time:
            return by_time[t]
    return None


def make_engine(config: Config) -> BookingEngine:
    """Convenience factory used by main.py and the scheduler."""
    from padel_booking.notifier import Notifier
    from padel_booking.state import StateStore

    state = StateStore(config.state_db_path)
    notifier = Notifier(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_user=config.smtp_user,
        smtp_password=config.smtp_password,
        smtp_from=config.smtp_from,
        notification_to=config.notification_to,
    )
    return BookingEngine(config, state, notifier)
