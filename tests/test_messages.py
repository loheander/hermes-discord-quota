from datetime import datetime, timezone
from decimal import Decimal

from discord_quota.config import Limits, QuotaConfig
from discord_quota.messages import (append_footer, blocked_reply, quota_exceeded_reply,
                                    quota_status_text, reset_phrase, warning_footer)
from discord_quota.status import evaluate

Z = "Europe/Stockholm"
# Monday 2026-10-26 15:00 UTC = 16:00 local: daily reset tomorrow, weekly Friday.
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
RULE = "─" * 40


class L:
    def __init__(self, day, week):
        self.day, self.week = Decimal(day), Decimal(week)

    def spend_since(self, user, start, end=None):
        return self.week if (end - start).days == 7 else self.day


def st(d, w, day, week, now=NOW):
    return evaluate("u", QuotaConfig(enabled=True, default=Limits(d, w)), L(day, week), now)


def test_reset_phrases():
    mk = lambda *a: datetime(*a, 12, tzinfo=__import__("zoneinfo").ZoneInfo(Z))
    assert reset_phrase(mk(2026, 10, 26), mk(2026, 10, 26), Z) == "Today at 12:00 Europe/Stockholm"
    assert reset_phrase(mk(2026, 10, 27), NOW, Z) == "Tomorrow at 12:00 Europe/Stockholm"
    assert reset_phrase(mk(2026, 10, 30), NOW, Z) == "Friday at 12:00 Europe/Stockholm"
    assert reset_phrase(mk(2026, 11, 2), NOW, Z) == "Next Monday at 12:00 Europe/Stockholm"


def test_exceeded_reply_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)  # 09:00 local, before noon
    s = st(1.0, 5.0, "1.04", "3.18", now)
    assert quota_exceeded_reply(s, now, Z) == (
        "⚠️ Discord Quota Exceeded\n\n"
        "Your Daily spending limit has been reached:\n"
        "• Daily spend:  $1.04 / $1.00 (104%) — resets Today at 12:00 Europe/Stockholm\n"
        "• Weekly spend: $3.18 / $5.00 (63%) — resets Friday at 12:00 Europe/Stockholm\n\n"
        "Further requests are paused until the next reset. Run /quota to check your status.")


def test_blocked():
    s = st(0.0, 5.0, "0", "0")
    assert quota_exceeded_reply(s, NOW, Z) == blocked_reply()
    assert blocked_reply().startswith("🚫 Access Blocked\n\nYou do not have")


def test_status_text_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    s = st(1.0, 5.0, "0.42", "1.85", now)
    assert quota_status_text(s, now, Z) == (
        "📊 Discord Quota Status\n\n"
        "• Daily Quota:  $0.58 / $1.00 remaining (42% used — resets Today at 12:00 Europe/Stockholm)\n"
        "• Weekly Quota: $3.15 / $5.00 remaining (37% used — resets Friday at 12:00 Europe/Stockholm)\n"
        "• Status: Active")


def test_unlimited_shown():
    s = st(-1, -1, "0.5", "0.5")
    t = quota_status_text(s, NOW, Z)
    assert "unlimited ($0.50 spent" in t and "Status: Active" in t
    assert warning_footer(s, NOW, Z) is None


def test_footers():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert warning_footer(st(1.0, 5.0, "0.92", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Quota Warning: You have used 92% of your Daily budget ($0.92 / $1.00). "
        "Resets at 12:00 Europe/Stockholm.")
    assert warning_footer(st(1.0, 5.0, "1.04", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Quota Exhausted: Daily spend is $1.04 / $1.00 (104%). "
        "Further requests are paused until Today at 12:00 Europe/Stockholm.")
    assert warning_footer(st(1.0, 5.0, "0.1", "1", now), now, Z) is None
    assert warning_footer(st(1.0, 5.0, "0.1", "4.6", now), now, Z).endswith("Resets Friday at 12:00 Europe/Stockholm.")


def test_pct_floors_and_deterministic():
    s = st(1.0, 5.0, "0.999", "0")
    assert "99%" in warning_footer(s, NOW, Z)
    assert warning_footer(s, NOW, Z) == warning_footer(s, NOW, Z)
    assert append_footer("hi \n", "F") == "hi\n\nF" and append_footer("hi", None) is None
