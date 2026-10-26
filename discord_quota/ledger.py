"""Append-only SQLite spend ledger (PLAN.md section 5, invariant 4).

Spend per window is computed statelessly by summing rows. Nothing touches the
filesystem at import time; the DB file is created on first use. Each operation
opens its own short-lived connection (safe across gateway and agent threads),
and writes are serialised in-process by a lock on top of SQLite's WAL locking.

Calls are synchronous (process lock, up to 30s busy timeout): async callers
must offload them with ``asyncio.to_thread``. Construct the Ledger once, with
an explicit path, at register time: ``get_hermes_home()`` is context-local and
the resolved default path is cached on first use.

Naive datetimes are interpreted as UTC (consistent with windows.py). Credits
are negative rows, so a credit also reduces every overlapping window (a daily
credit lowers the weekly sum too) and sums can go negative.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_NAME = "discord_quota.db"
_QUANT = Decimal("0.000001")
_MICRO = Decimal(1_000_000)

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    timestamp REAL NOT NULL,
    cost_usd REAL NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_kind TEXT DEFAULT 'api',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trans_user_time ON quota_transactions(user_id, timestamp);
"""


def round_cost(value: Any) -> Decimal:
    """Round a cost to 6 decimal places (half up) via Decimal."""
    if isinstance(value, float):
        value = repr(value)
    return Decimal(value).quantize(_QUANT, rounding=ROUND_HALF_UP)


def default_db_path() -> Path:
    from hermes_constants import get_hermes_home  # lazy: Hermes may be absent

    return Path(get_hermes_home()) / DB_NAME


def _epoch(ts: Any) -> float:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    return float(ts)


@dataclass(frozen=True)
class Transaction:
    id: int
    user_id: str
    session_id: str
    turn_id: str | None
    timestamp: float
    cost_usd: Decimal
    model: str
    provider: str
    source_kind: str
    input_tokens: int
    output_tokens: int


class Ledger:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._ready = False

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_db_path()
        return self._path

    def _connect(self) -> sqlite3.Connection:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            if not self._ready:
                with self._lock:
                    if not self._ready:
                        conn.execute("PRAGMA journal_mode = WAL")
                        conn.executescript(SCHEMA)
                        self._ready = True
            conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            conn.close()
            raise
        return conn

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(sql, params)
                    result = cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                return result
            finally:
                conn.close()

    def record(self, user_id: str, session_id: str, cost_usd: Any, model: str,
               provider: str, *, turn_id: str | None = None,
               timestamp: Any = None, source_kind: str = "api",
               input_tokens: int = 0, output_tokens: int = 0) -> int:
        ts = time.time() if timestamp is None else _epoch(timestamp)
        try:
            cost = round_cost(cost_usd)
            if not cost.is_finite():
                raise ValueError("non-finite")
        except (ArithmeticError, ValueError, TypeError):
            logger.warning("discord_quota: invalid cost %r for user %s; recording 0", cost_usd, user_id)
            cost = Decimal(0)
        return self._write(
            "INSERT INTO quota_transactions (user_id, session_id, turn_id, timestamp,"
            " cost_usd, model, provider, source_kind, input_tokens, output_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(user_id), session_id, turn_id, ts, float(cost), model,
             provider, source_kind, int(input_tokens), int(output_tokens)))

    def spend_since(self, user_id: str, start: Any, end: Any = None) -> Decimal:
        """Sum of costs (credits are negative) with timestamp >= start (and < end)."""
        sql = ("SELECT COALESCE(SUM(CAST(ROUND(cost_usd * 1000000) AS INTEGER)), 0)"
               " FROM quota_transactions WHERE user_id = ? AND timestamp >= ?")
        params: list[Any] = [str(user_id), _epoch(start)]
        if end is not None:
            sql += " AND timestamp < ?"
            params.append(_epoch(end))
        conn = self._connect()
        try:
            micros = conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()
        return (Decimal(int(micros)) / _MICRO).quantize(_QUANT)

    def recent(self, user_id: str, n: int = 10) -> list[Transaction]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, user_id, session_id, turn_id, timestamp, cost_usd, model,"
                " provider, source_kind, input_tokens, output_tokens"
                " FROM quota_transactions WHERE user_id = ?"
                " ORDER BY timestamp DESC, id DESC LIMIT ?", (str(user_id), int(n))).fetchall()
        finally:
            conn.close()
        return [Transaction(r[0], r[1], r[2], r[3], r[4], round_cost(r[5]), *r[6:]) for r in rows]

    def delete_window(self, user_id: str, start: Any, end: Any = None) -> int:
        """Delete a user's rows in [start, end); returns the number deleted."""
        sql = "DELETE FROM quota_transactions WHERE user_id = ? AND timestamp >= ?"
        params: list[Any] = [str(user_id), _epoch(start)]
        if end is not None:
            sql += " AND timestamp < ?"
            params.append(_epoch(end))
        return self._write(sql, tuple(params))

    def add_credit(self, user_id: str, amount: Any, *, session_id: str = "admin",
                   timestamp: Any = None, source_kind: str = "admin_reset") -> int:
        """Insert an offsetting negative row (amount given as a positive USD value)."""
        return self.record(user_id, session_id, -abs(round_cost(amount)), "admin", "admin",
                           timestamp=timestamp, source_kind=source_kind)

    def reset_window(self, user_id: str, start: Any, end: Any = None, *,
                     timestamp: Any = None) -> Decimal:
        """Atomically offset the window spend with a credit row; returns the amount credited.

        Sum and insert happen in one BEGIN IMMEDIATE transaction. The credit
        timestamp is clamped into [start, end) so it reduces that window. Being
        a negative row it also reduces any overlapping window (a daily credit
        lowers the weekly sum) and sums can go negative.
        """
        start_ts = _epoch(start)
        end_ts = _epoch(end) if end is not None else None
        ts = time.time() if timestamp is None else _epoch(timestamp)
        if end_ts is not None:
            ts = min(ts, end_ts - 1e-6)
        ts = max(ts, start_ts)
        sql = ("SELECT COALESCE(SUM(CAST(ROUND(cost_usd * 1000000) AS INTEGER)), 0)"
               " FROM quota_transactions WHERE user_id = ? AND timestamp >= ?")
        params: list[Any] = [str(user_id), start_ts]
        if end_ts is not None:
            sql += " AND timestamp < ?"
            params.append(end_ts)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    micros = int(conn.execute(sql, params).fetchone()[0])
                    if micros > 0:
                        conn.execute(
                            "INSERT INTO quota_transactions (user_id, session_id, timestamp,"
                            " cost_usd, model, provider, source_kind)"
                            " VALUES (?, 'admin', ?, ?, 'admin', 'admin', 'admin_reset')",
                            (str(user_id), ts, -micros / 1_000_000))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return Decimal(max(micros, 0)) / _MICRO
