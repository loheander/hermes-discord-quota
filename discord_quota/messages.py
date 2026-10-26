"""Deterministic canned replies and footers (PLAN.md sections 6.6, 7.1-7.3).

Pure functions over ``QuotaStatus``; ``now`` and the zone name are injected.
All user-facing text is Swedish (issue #37). Amounts are shown to 2 dp (half-up)
with a decimal comma and the currency after the number ("0,45 USD"); percentages
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
    "🚫 Åtkomst blockerad\n\n"
    "Du har ingen aktiv kostnadskvot för den här boten.\n"
    "Kontakta botens administratör för att be om åtkomst."
)

UNAVAILABLE_REPLY = "Kvotkontrollen är tillfälligt otillgänglig, försök igen senare."

CONCURRENT_REPLY = (
    "En annan förfrågan från dig pågår fortfarande och din återstående kvot är låg "
    "({remaining}). Vänta tills den är klar och försök igen."
)

_CENT = Decimal("0.01")


def usd(v: Decimal) -> str:
    q = Decimal(v).quantize(_CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # no "$-0.00"
    text = f"{abs(q)}".replace(".", ",")
    return f"-{text} USD" if q < 0 else f"{text} USD"


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


_ADJ = {"daily": "dagliga", "weekly": "veckovisa"}
_POSS = {"daily": "Dagens", "weekly": "Veckans"}


def _adj(w: WindowStatus) -> str:
    return _ADJ.get(w.name, w.name)


def _poss(w: WindowStatus) -> str:
    return _POSS.get(w.name, w.name.capitalize())


def _spend(w: WindowStatus) -> str:
    if w.state is WindowState.UNLIMITED:
        return f"{usd(w.spent)} / obegränsad"
    if w.state is WindowState.BLOCKED:
        return f"{usd(w.spent)} / {usd(w.limit)} (blockerad)"
    return f"{usd(w.spent)} / {usd(w.limit)} ({pct(w)})"


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
    names = [_adj(w) for w in status.blocking]
    if not names:  # admitted or disabled: never raise inside hooks
        return "⚠️ Kvoten är slut\n\nKör /quota för att se din status."
    if len(names) == 1:
        head = f"Din {names[0]} kostnadsgräns är nådd:"
    else:
        head = f"Dina {' och '.join(names)} kostnadsgränser är nådda:"
    lines = []
    for w, label in zip(status.windows, ("Dagens kostnad:", "Veckans kostnad:")):
        lines.append(f"• {label} {_spend(w)} — återställs {reset_phrase(w.resets_at, now, zone)}")
    return (
        "⚠️ Kvoten är slut\n\n"
        f"{head}\n" + "\n".join(lines) + "\n\n"
        "Nya förfrågningar pausas till nästa återställning. Kör /quota för att se din status."
    )


def _status_label(status: QuotaStatus) -> str:
    if not status.enabled:
        return "Avstängd"
    if is_blocked(status):
        return "Blockerad"
    if status.exhausted:
        return "Överskriden"
    if status.warn:
        return "Varning"
    return "Aktiv"


def _quota_line(w: WindowStatus, now: datetime, zone: str) -> str:
    resets = reset_phrase(w.resets_at, now, zone)
    if w.state is WindowState.UNLIMITED:
        return f"obegränsad ({usd(w.spent)} förbrukat — återställs {resets})"
    if w.state is WindowState.BLOCKED:
        return f"ingen, åtkomst blockerad ({usd(w.spent)} förbrukat — återställs {resets})"
    return f"{usd(w.remaining)} / {usd(w.limit)} kvar ({pct(w)} använt — återställs {resets})"


def quota_status_text(status: QuotaStatus, now: datetime, zone: str) -> str:
    """PLAN.md 7.3."""
    return (
        "📊 Kvotstatus för Discord\n\n"
        f"• Dagskvot:  {_quota_line(status.daily, now, zone)}\n"
        f"• Veckokvot: {_quota_line(status.weekly, now, zone)}\n"
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
            f"{FOOTER_RULE}\n⚠️ Kvoten är slut: {_poss(w)} kostnad är "
            f"{usd(w.spent)} / {usd(w.limit)} ({pct(w)}). Nya förfrågningar pausas "
            f"till {reset_phrase(until, now, zone)}."
        )
    warned = [w for w in status.windows if w.warn]
    if not warned:
        return None
    w = max(warned, key=lambda x: (x.percent or 0))
    return (
        f"{FOOTER_RULE}\n⚠️ Kvotvarning: Du har använt {pct(w)} av din "
        f"{_adj(w)} budget ({usd(w.spent)} / {usd(w.limit)}). "
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
