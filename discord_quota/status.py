"""Pure quota status evaluation (PLAN.md section 4.2).

``evaluate`` is the single decision point: config, ledger and ``now`` are
injected. Money comparisons use ``Decimal``. Ledger sums can be negative after
credits: ``remaining`` is clamped at 0 from below only (a credit raises it
above the limit), and ``percent`` is clamped at 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from .config import QuotaConfig
from .windows import daily_window, weekly_window

_ZERO = Decimal(0)


class WindowState(str, Enum):
    UNLIMITED = "unlimited"
    BLOCKED = "blocked"      # limit == 0: never allowed
    ACTIVE = "active"        # under the warn threshold
    WARN = "warn"            # threshold <= S/Q < 1
    EXHAUSTED = "exhausted"  # S >= Q > 0


@dataclass(frozen=True)
class WindowStatus:
    name: str                  # "daily" or "weekly"
    limit: Decimal             # Q (negative = unlimited, 0 = blocked)
    spent: Decimal             # S (may be negative after credits)
    remaining: Decimal | None  # max(0, Q - S); None when unlimited
    percent: Decimal | None    # max(0, S / Q * 100); None unless Q > 0
    start: datetime
    resets_at: datetime
    state: WindowState

    @property
    def allowed(self) -> bool:
        return self.state in (WindowState.UNLIMITED, WindowState.ACTIVE, WindowState.WARN)

    @property
    def warn(self) -> bool:
        return self.state is WindowState.WARN


@dataclass(frozen=True)
class QuotaStatus:
    user_id: str
    daily: WindowStatus
    weekly: WindowStatus
    enabled: bool = True

    @property
    def admitted(self) -> bool:
        return not self.enabled or (self.daily.allowed and self.weekly.allowed)

    @property
    def warn(self) -> bool:
        return self.enabled and (self.daily.warn or self.weekly.warn)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def window_status(name: str, limit: float, spent: Decimal, start: datetime,
                  resets_at: datetime, warn_threshold: float) -> WindowStatus:
    q = _dec(limit)
    remaining: Decimal | None = None
    percent: Decimal | None = None
    if q < 0:
        state = WindowState.UNLIMITED
    elif q == 0:
        state, remaining = WindowState.BLOCKED, _ZERO
    else:
        remaining = max(_ZERO, q - spent)
        percent = max(_ZERO, spent / q * 100)
        if spent >= q:
            state = WindowState.EXHAUSTED
        elif spent >= _dec(warn_threshold) * q:
            state = WindowState.WARN
        else:
            state = WindowState.ACTIVE
    return WindowStatus(name, q, spent, remaining, percent, start, resets_at, state)


def evaluate(user_id: Any, config: QuotaConfig, ledger: Any,
             now: datetime | None = None) -> QuotaStatus:
    """Evaluate both windows for ``user_id``. Naive ``now`` means UTC."""
    if now is None:
        now = datetime.now(timezone.utc)
    uid = str(user_id)
    q_day, q_week = config.limits_for(uid)
    d_start, d_next = daily_window(now, config.timezone)
    w_start, w_next = weekly_window(now, config.timezone)
    s_day = ledger.spend_since(uid, d_start, d_next)
    s_week = ledger.spend_since(uid, w_start, w_next)
    return QuotaStatus(
        uid,
        window_status("daily", q_day, s_day, d_start, d_next, config.warn_threshold),
        window_status("weekly", q_week, s_week, w_start, w_next, config.warn_threshold),
        enabled=config.enabled,
    )
