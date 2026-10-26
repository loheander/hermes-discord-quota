"""Deterministic canned replies and footers (PLAN.md sections 6.6, 7.1-7.3).

Pure functions over ``QuotaStatus``; ``now`` and the zone name are injected.
All user-facing text is Swedish (issue #37). Amounts are shown to 2 dp (half-up)
with a decimal comma and "kr" after the number ("4,50 kr"); USD ledger values are
converted at a fixed rate (SEK_PER_USD) and rounded half-up on the SEK value; percentages
are half-up integers, capped at 99% unless the window is EXHAUSTED so an
under-limit window never reads "100%". Unlimited windows show "obegränsad".
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .status import QuotaStatus, WindowState, WindowStatus

FOOTER_RULE = "─" * 40

BLOCKED_REPLY = (
    "🚫 Ingen åtkomst\n\n"
    "Du har ingen budget för den här boten.\n"
    "Fråga den som driver boten om du kan få använda den."
)

UNAVAILABLE_REPLY = "Kan inte kolla hur mycket pengar du har kvar, försök senare."

CONCURRENT_REPLY = (
    "Du har en fråga som fortfarande pågår och din återstående budget är låg "
    "({remaining}). Vänta tills den är klar och försök igen."
)

SEK_PER_USD = Decimal(10)

_CENT = Decimal("0.01")


def usd(v: Decimal) -> str:
    """Format a USD ledger amount as SEK: "4,50 kr" (fixed rate, half-up on SEK)."""
    q = (Decimal(v) * SEK_PER_USD).quantize(_CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # no "-0,00 kr"
    text = f"{abs(q)}".replace(".", ",")
    return f"-{text} kr" if q < 0 else f"{text} kr"


def pct(w: WindowStatus) -> str:
    """Half-up integer percent, capped at 99 unless the window is EXHAUSTED."""
    n = int((w.percent or Decimal(0)).to_integral_value(rounding=ROUND_HALF_UP))
    if w.state is not WindowState.EXHAUSTED:
        n = min(n, 99)
    return f"{n}%"


_WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")


def _local(dt: datetime, now: datetime, zone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz), now.astimezone(tz)


def reset_phrase(resets_at: datetime, now: datetime, zone: str) -> str:
    """"idag kl. 12:00", "imorgon kl. 12:00", "fredag kl. 12:00", "nästa fredag kl. 12:00"."""
    r, n = _local(resets_at, now, zone)
    days = (r.date() - n.date()).days
    clock = f"{r:%H:%M}"
    if days <= 0:
        day = "idag"
    elif days == 1:
        day = "imorgon"
    elif days >= 7:
        day = f"nästa {_WEEKDAYS[r.weekday()]}"
    else:
        day = f"{_WEEKDAYS[r.weekday()]}"
    return f"{day} kl. {clock}"


_PER = {"daily": "per dag", "weekly": "per vecka"}


def _per(w: WindowStatus) -> str:
    return _PER.get(w.name, w.name)


def _spend(w: WindowStatus) -> str:
    if w.state is WindowState.UNLIMITED:
        return f"{usd(w.spent)} (ingen gräns)"
    if w.state is WindowState.BLOCKED:
        return f"{usd(w.spent)} av {usd(w.limit)} (blockerad)"
    return f"{usd(w.spent)} av {usd(w.limit)} ({pct(w)})"


def is_blocked(status: QuotaStatus) -> bool:
    return any(w.state is WindowState.BLOCKED for w in status.blocking)


def unavailable_reply() -> str:
    return UNAVAILABLE_REPLY


def concurrent_reply(remaining: Decimal) -> str:
    """PLAN.md 6.1 step 5: parallel turn rejected on a low balance."""
    return CONCURRENT_REPLY.format(remaining=usd(remaining))


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
    names = [_per(w) for w in status.blocking]
    if not names:  # admitted or disabled: never raise inside hooks
        return "⚠️ Pengarna är slut\n\nSkriv /quota för att se hur mycket du har kvar."
    head = f"Du har använt upp din budget {' och '.join(names)}:"
    lines = []
    for w in status.windows:
        lines.append(f"• {_per(w).capitalize()}: {_spend(w)} — återställs {reset_phrase(w.resets_at, now, zone)}")
    return (
        "⚠️ Pengarna är slut\n\n"
        f"{head}\n" + "\n".join(lines) + "\n\n"
        "Du kan inte skriva till boten förrän det återställs. Skriv /quota för att se hur mycket du har kvar."
    )


def _status_label(status: QuotaStatus) -> str:
    if not status.enabled:
        return "Av"
    if is_blocked(status):
        return "Blockerad"
    if status.exhausted:
        return "Slut"
    if status.warn:
        return "Varning"
    return "Ok"


def _quota_line(w: WindowStatus, now: datetime, zone: str) -> str:
    resets = reset_phrase(w.resets_at, now, zone)
    if w.state is WindowState.UNLIMITED:
        return f"ingen gräns ({usd(w.spent)} använt — återställs {resets})"
    if w.state is WindowState.BLOCKED:
        return f"ingen budget, ingen åtkomst ({usd(w.spent)} använt — återställs {resets})"
    return f"{usd(w.remaining)} kvar av {usd(w.limit)} ({pct(w)} använt — återställs {resets})"


def quota_status_text(status: QuotaStatus, now: datetime, zone: str) -> str:
    """PLAN.md 7.3."""
    return (
        "📊 Din budget\n\n"
        f"• Per dag: {_quota_line(status.daily, now, zone)}\n"
        f"• Per vecka: {_quota_line(status.weekly, now, zone)}\n"
        f"• Läge: {_status_label(status)}"
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
            f"{FOOTER_RULE}\n⚠️ Pengarna är slut: du har använt {usd(w.spent)} av "
            f"{usd(w.limit)} {_per(w)} ({pct(w)}). Du kan skriva igen "
            f"{reset_phrase(until, now, zone)}."
        )
    warned = [w for w in status.windows if w.warn]
    if not warned:
        return None
    w = max(warned, key=lambda x: (x.percent or 0))
    return (
        f"{FOOTER_RULE}\n⚠️ Budgetvarning: Du har använt {pct(w)} av din budget "
        f"{_per(w)} ({usd(w.spent)} av {usd(w.limit)}). "
        f"Återställs {_reset_short(w, now, zone)}."
    )


def _reset_short(w: WindowStatus, now: datetime, zone: str) -> str:
    """"kl. 12:00" when resetting today (PLAN 6.6), else with the day name."""
    phrase = reset_phrase(w.resets_at, now, zone)
    return phrase[len("idag "):] if phrase.startswith("idag ") else phrase


def append_footer(response_text: str, footer: str | None) -> str | None:
    """PLAN.md 6.6 step 4; None when nothing to append."""
    if footer is None:
        return None
    return f"{response_text.rstrip()}\n\n{footer}"
