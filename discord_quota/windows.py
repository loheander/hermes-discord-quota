"""Pure reset-window boundaries (PLAN.md section 4.1).

Daily windows refill at 12:00 local wall-clock time every day, weekly windows
every Friday at 12:00. ``now`` and the zone are injected. A naive ``now`` is
interpreted as UTC (NOT system-local), so callers should pass aware datetimes,
e.g. ``datetime.fromtimestamp(ts, timezone.utc)`` for epoch floats; passing
``datetime.now()`` on a non-UTC host would shift the windows. Results are aware
datetimes in the configured zone.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

RESET_HOUR = 12
FRIDAY = 4


def _zone(tz: str | tzinfo) -> tzinfo:
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _local(now: datetime, tz: tzinfo) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz)


def _noon(local: datetime, days: int, tz: tzinfo) -> datetime:
    """12:00 wall-clock on ``local``'s date shifted by ``days`` calendar days."""
    day = local.date() + timedelta(days=days)
    return datetime.combine(day, time(RESET_HOUR), tzinfo=tz)


def daily_window(now: datetime, tz: str | tzinfo) -> tuple[datetime, datetime]:
    """Return ``(start, next)`` of the window containing ``now``.

    Naive ``now`` is treated as UTC, not system-local; pass an aware datetime
    (e.g. ``datetime.fromtimestamp(ts, timezone.utc)``).
    """
    zone = _zone(tz)
    local = _local(now, zone)
    start = _noon(local, 0, zone)
    if local < start:
        start = _noon(local, -1, zone)
    nxt = datetime.combine(start.date() + timedelta(days=1), time(RESET_HOUR), tzinfo=zone)
    return start, nxt


def weekly_window(now: datetime, tz: str | tzinfo) -> tuple[datetime, datetime]:
    """Return ``(start, next)`` of the window containing ``now``.

    Naive ``now`` is treated as UTC, not system-local; pass an aware datetime
    (e.g. ``datetime.fromtimestamp(ts, timezone.utc)``).
    """
    zone = _zone(tz)
    local = _local(now, zone)
    back = (local.weekday() - FRIDAY) % 7
    start = _noon(local, -back, zone)
    if local < start:
        start = _noon(local, -back - 7, zone)
    nxt = datetime.combine(start.date() + timedelta(days=7), time(RESET_HOUR), tzinfo=zone)
    return start, nxt
