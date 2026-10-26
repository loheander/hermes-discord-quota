"""Deterministic canned replies and footers (PLAN.md sections 6.6, 7.1-7.3).

Pure functions over ``QuotaStatus``; ``now`` and the zone name are injected.
Dollars are shown to 2 dp (half-up); percentages are half-up integers, capped at
99% unless the window is EXHAUSTED so an under-limit window never reads "100%".
Unlimited windows show "unlimited".
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .status import QuotaStatus, WindowState, WindowStatus

FOOTER_RULE = "─" * 40

BLOCKED_REPLY = (
    "🚫 Access Blocked\n\n"
    "You do not have an active spending quota for this bot.\n"
    "Please contact the bot administrator to request access."
)

_CENT = Decimal("0.01")


def usd(v: Decimal) -> str:
    q = Decimal(v).quantize(_CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # no "$-0.00"
    return f"-${-q}" if q < 0 else f"${q}"


def pct(w: WindowStatus) -> str:
    """Half-up integer percent, capped at 99 unless the window is EXHAUSTED."""
    n = int((w.percent or Decimal(0)).to_integral_value(rounding=ROUND_HALF_UP))
    if w.state is not WindowState.EXHAUSTED:
        n = min(n, 99)
    return f"{n}%"


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _local(dt: datetime, now: datetime, zone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz), now.astimezone(tz)


def reset_phrase(resets_at: datetime, now: datetime, zone: str) -> str:
    """"Today at 12:00 Europe/Stockholm", "Tomorrow at ...", "Friday at ..."."""
    r, n = _local(resets_at, now, zone)
    days = (r.date() - n.date()).days
    clock = f"{r:%H:%M} {zone}"
    if days <= 0:
        day = "Today"
    elif days == 1:
        day = "Tomorrow"
    elif days >= 7:
        day = f"Next {_WEEKDAYS[r.weekday()]}"
    else:
        day = f"{_WEEKDAYS[r.weekday()]}"
    return f"{day} at {clock}"


def _title(w: WindowStatus) -> str:
    return w.name.capitalize()


def _spend(w: WindowStatus) -> str:
    if w.state is WindowState.UNLIMITED:
        return f"{usd(w.spent)} / unlimited"
    if w.state is WindowState.BLOCKED:
        return f"{usd(w.spent)} / {usd(w.limit)} (blocked)"
    return f"{usd(w.spent)} / {usd(w.limit)} ({pct(w)})"


def is_blocked(status: QuotaStatus) -> bool:
    return any(w.state is WindowState.BLOCKED for w in status.blocking)


def blocked_reply() -> str:
    """PLAN.md 7.2."""
    return BLOCKED_REPLY


def quota_exceeded_reply(status: QuotaStatus, now: datetime, zone: str) -> str:
    """PLAN.md 7.1.

    If any window is blocked (limit 0) the 7.2 blocked reply takes precedence,
    even when the other window is exhausted. With nothing blocking (admitted or
    disabled status) a short generic reply is returned; this never raises.
    """
    if is_blocked(status):
        return BLOCKED_REPLY
    names = [_title(w) for w in status.blocking]
    if not names:  # admitted or disabled: never raise inside hooks
        return "⚠️ Discord Quota Exceeded\n\nRun /quota to check your status."
    if len(names) == 1:
        head = f"Your {names[0]} spending limit has been reached:"
    else:
        head = f"Your {' and '.join(names)} spending limits have been reached:"
    lines = []
    for w, label in zip(status.windows, ("Daily spend: ", "Weekly spend:")):
        lines.append(f"• {label} {_spend(w)} — resets {reset_phrase(w.resets_at, now, zone)}")
    return (
        "⚠️ Discord Quota Exceeded\n\n"
        f"{head}\n" + "\n".join(lines) + "\n\n"
        "Further requests are paused until the next reset. Run /quota to check your status."
    )


def _status_label(status: QuotaStatus) -> str:
    if not status.enabled:
        return "Disabled"
    if is_blocked(status):
        return "Blocked"
    if status.exhausted:
        return "Exceeded"
    if status.warn:
        return "Warning"
    return "Active"


def _quota_line(w: WindowStatus, now: datetime, zone: str) -> str:
    resets = reset_phrase(w.resets_at, now, zone)
    if w.state is WindowState.UNLIMITED:
        return f"unlimited ({usd(w.spent)} spent — resets {resets})"
    if w.state is WindowState.BLOCKED:
        return f"none, access blocked ({usd(w.spent)} spent — resets {resets})"
    return f"{usd(w.remaining)} / {usd(w.limit)} remaining ({pct(w)} used — resets {resets})"


def quota_status_text(status: QuotaStatus, now: datetime, zone: str) -> str:
    """PLAN.md 7.3."""
    return (
        "📊 Discord Quota Status\n\n"
        f"• Daily Quota:  {_quota_line(status.daily, now, zone)}\n"
        f"• Weekly Quota: {_quota_line(status.weekly, now, zone)}\n"
        f"• Status: {_status_label(status)}"
    )


def warning_footer(status: QuotaStatus, now: datetime, zone: str) -> str | None:
    """PLAN.md 6.6 footers: exhausted (case A) wins over warning (case B); else None."""
    if not status.enabled:
        return None
    if status.exhausted:
        ex = [w for w in status.windows if w.state is WindowState.EXHAUSTED]
        until = max(w.resets_at for w in ex)
        w = max(ex, key=lambda x: (x.percent or 0))
        return (
            f"{FOOTER_RULE}\n⚠️ Quota Exhausted: {_title(w)} spend is "
            f"{usd(w.spent)} / {usd(w.limit)} ({pct(w)}). Further requests are "
            f"paused until {reset_phrase(until, now, zone)}."
        )
    warned = [w for w in status.windows if w.warn]
    if not warned:
        return None
    w = max(warned, key=lambda x: (x.percent or 0))
    return (
        f"{FOOTER_RULE}\n⚠️ Quota Warning: You have used {pct(w)} of your "
        f"{_title(w)} budget ({usd(w.spent)} / {usd(w.limit)}). "
        f"Resets {_reset_short(w, now, zone)}."
    )


def _reset_short(w: WindowStatus, now: datetime, zone: str) -> str:
    """"at 12:00 Zone" when resetting today (PLAN 6.6), else with the day name."""
    phrase = reset_phrase(w.resets_at, now, zone)
    return phrase[len("Today "):] if phrase.startswith("Today ") else phrase


def append_footer(response_text: str, footer: str | None) -> str | None:
    """PLAN.md 6.6 step 4; None when nothing to append."""
    if footer is None:
        return None
    return f"{response_text.rstrip()}\n\n{footer}"
