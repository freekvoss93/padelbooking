"""
Email notification layer.

Uses stdlib smtplib with STARTTLS (works with Gmail app-passwords,
SendGrid SMTP relay, Mailgun, or any standard SMTP server).
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notification context
# ---------------------------------------------------------------------------

@dataclass
class NotificationContext:
    event: str           # booking_success | no_slots | insufficient_credits |
                         # payment_required | error
    trigger_date: str
    target_date: str
    club_name: str
    dry_run: bool = False

    start_time: Optional[str] = None
    end_time: Optional[str] = None
    court_name: Optional[str] = None
    reservation_reference: Optional[str] = None
    total_price: Optional[float] = None
    credit_balance: Optional[float] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        smtp_from: str,
        notification_to: str,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from or smtp_user
        self.notification_to = notification_to

    def send(self, ctx: NotificationContext) -> None:
        subject, body = _render(ctx)
        prefix = "[DRY RUN] " if ctx.dry_run else ""
        full_subject = prefix + subject

        if not self.notification_to or not self.smtp_user:
            log.warning(
                "Notification skipped (SMTP not configured) — subject: %s", full_subject
            )
            return

        if ctx.dry_run:
            log.info("DRY RUN — would send: %s", full_subject)
            log.debug("Body:\n%s", body)
            return

        try:
            self._send_smtp(full_subject, body)
            log.info("Notification sent: event=%s subject=%r", ctx.event, full_subject)
        except Exception as exc:
            # Notification failure must never crash the booking flow
            log.error("Failed to send notification: %s", exc, exc_info=True)

    def _send_smtp(self, subject: str, body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = self.notification_to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_from, [self.notification_to], msg.as_string())


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _render(ctx: NotificationContext) -> tuple[str, str]:
    bal = f"€{ctx.credit_balance:.2f}" if ctx.credit_balance is not None else "unknown"

    if ctx.event == "booking_success":
        price = f"€{ctx.total_price:.2f}" if ctx.total_price is not None else "unknown"
        return (
            f"✅ Padel booked — {ctx.target_date} {ctx.start_time}",
            f"""Your padel court has been reserved successfully.

Club:      {ctx.club_name}
Date:      {ctx.target_date}
Time:      {ctx.start_time} – {ctx.end_time}
Court:     {ctx.court_name or "any available"}
Reference: {ctx.reservation_reference or "n/a"}
Price:     {price} (paid with credits)
Balance:   {bal} remaining

Triggered: {ctx.trigger_date}
""",
        )

    if ctx.event == "no_slots":
        return (
            f"⚠️ No padel slots — {ctx.target_date}",
            f"""No courts were available for your preferred times.

Club:   {ctx.club_name}
Date:   {ctx.target_date}
Tried:  {", ".join(["20:00", "19:30", "19:00"])} (all unavailable)

Book manually: https://peakzpadel.nl/reserveren
""",
        )

    if ctx.event == "insufficient_credits":
        return (
            f"💳 Booking skipped — low credits ({bal})",
            f"""Your credit balance is below the configured minimum.

Club:    {ctx.club_name}
Date:    {ctx.target_date}
Balance: {bal}

Top up your credits and either wait for next week's run,
or trigger a manual run after topping up:
  python main.py run-now

""",
        )

    if ctx.event == "payment_required":
        return (
            f"💳 Manual payment needed — {ctx.target_date} {ctx.start_time}",
            f"""A slot was found but payment could not be completed automatically.

Club:  {ctx.club_name}
Date:  {ctx.target_date}
Time:  {ctx.start_time}
Court: {ctx.court_name or "n/a"}

Please complete payment manually:
  https://peakzpadel.nl/reserveren
""",
        )

    if ctx.event == "error":
        return (
            f"❌ Booking failed — {ctx.trigger_date}",
            f"""An error occurred during the automated booking.

Club:  {ctx.club_name}
Date:  {ctx.target_date}
Error: {ctx.error_message}

Please check the logs and, if needed, book manually:
  https://peakzpadel.nl/reserveren

Log files: data/screenshots/ (if Playwright was used)
""",
        )

    # Generic fallback
    return (
        f"Padel booking event: {ctx.event}",
        f"Club: {ctx.club_name}\nDate: {ctx.target_date}\nEvent: {ctx.event}\n"
        f"Details: {ctx.error_message or '(none)'}",
    )
