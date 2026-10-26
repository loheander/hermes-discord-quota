"""Append-only SQLite spend ledger (PLAN.md section 5, invariant 4).

Spend per window is computed statelessly by summing rows. Nothing touches the
filesystem at import time; the DB file is created on first use. Each operation
opens its own short-lived connection (safe across gateway and agent threads),
and writes are serialised in-process by a lock on top of SQLite's WAL locking.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

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
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._ready:
            with self._lock:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                self._ready = True
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(sql, params)
                    return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
            finally:
                conn.close()

    def record(self, user_id: str, session_id: str, cost_usd: Any, model: str,
               provider: str, *, turn_id: str | None = None,
               timestamp: Any = None, source_kind: str = "api",
               input_tokens: int = 0, output_tokens: int = 0) -> int:
        ts = time.time() if timestamp is None else _epoch(timestamp)
        return self._write(
            "INSERT INTO quota_transactions (user_id, session_id, turn_id, timestamp,"
            " cost_usd, model, provider, source_kind, input_tokens, output_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(user_id), session_id, turn_id, ts, float(round_cost(cost_usd)), model,
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
        """Offset the current window spend with a credit row; returns the amount credited."""
        spent = self.spend_since(user_id, start, end)
        if spent > 0:
            self.add_credit(user_id, spent, timestamp=timestamp)
        return max(spent, Decimal(0))
