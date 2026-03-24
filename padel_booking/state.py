"""
SQLite-backed state store.

One row per weekly trigger — used for idempotency and audit trail.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BookingRun:
    trigger_date: date
    target_booking_date: date
    club_name: str
    # pending | success | no_slots | insufficient_credits | payment_required | error
    status: str

    court_id: Optional[str] = None
    court_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    booking_status: Optional[str] = None
    payment_status: Optional[str] = None
    credits_used: Optional[bool] = None
    credit_balance_before: Optional[float] = None
    credit_balance_after: Optional[float] = None
    reservation_reference: Optional[str] = None
    total_price: Optional[float] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class StateStore:
    _CREATE = """
        CREATE TABLE IF NOT EXISTS booking_runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_date         TEXT NOT NULL,
            target_booking_date  TEXT NOT NULL,
            club_name            TEXT NOT NULL,
            status               TEXT NOT NULL,
            court_id             TEXT,
            court_name           TEXT,
            start_time           TEXT,
            end_time             TEXT,
            booking_status       TEXT,
            payment_status       TEXT,
            credits_used         INTEGER,
            credit_balance_before REAL,
            credit_balance_after  REAL,
            reservation_reference TEXT,
            total_price          REAL,
            error_message        TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(self._CREATE)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Idempotency guards
    # ------------------------------------------------------------------

    def has_successful_booking(self, target_date: date) -> bool:
        """True if a successful booking for *target_date* already exists."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM booking_runs "
                "WHERE target_booking_date = ? AND status = 'success'",
                (target_date.isoformat(),),
            ).fetchone()
            return row is not None

    def has_run_today(self, trigger_date: date) -> bool:
        """True if any run (any status) already fired today."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM booking_runs WHERE trigger_date = ?",
                (trigger_date.isoformat(),),
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_run(self, run: BookingRun) -> int:
        now = _now()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO booking_runs (
                    trigger_date, target_booking_date, club_name, status,
                    court_id, court_name, start_time, end_time,
                    booking_status, payment_status, credits_used,
                    credit_balance_before, credit_balance_after,
                    reservation_reference, total_price, error_message,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.trigger_date.isoformat(),
                    run.target_booking_date.isoformat(),
                    run.club_name,
                    run.status,
                    run.court_id,
                    run.court_name,
                    run.start_time,
                    run.end_time,
                    run.booking_status,
                    run.payment_status,
                    _bool_to_int(run.credits_used),
                    run.credit_balance_before,
                    run.credit_balance_after,
                    run.reservation_reference,
                    run.total_price,
                    run.error_message,
                    now,
                    now,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def update_run(self, run_id: int, **kwargs) -> None:
        kwargs["updated_at"] = _now()
        if "credits_used" in kwargs:
            kwargs["credits_used"] = _bool_to_int(kwargs["credits_used"])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [run_id]
        with self._conn() as c:
            c.execute(f"UPDATE booking_runs SET {sets} WHERE id = ?", values)

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM booking_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _bool_to_int(v: Optional[bool]) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0
