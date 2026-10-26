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
        "• Weekly spend: $3.18 / $5.00 (64%) — resets Friday at 12:00 Europe/Stockholm\n\n"
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


def test_pct_rounds_half_up_caps_unless_exhausted():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert "(104%)" in warning_footer(st(1.0, 5.0, "1.044", "0", now), now, Z)
    assert "(100%)" in warning_footer(st(1.0, 5.0, "1.00", "0", now), now, Z)


def test_weekday_names_fixed_english():
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)  # Monday
    got = []
    for i in range(2, 9):
        r = datetime(2026, 10, 26, 12, tzinfo=ZoneInfo(Z)) + timedelta(days=i)
        got.append(reset_phrase(r, now, Z).split(" at ")[0])
    assert got == ["Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
                   "Next Monday", "Next Tuesday"]
    mon = reset_phrase(datetime(2026, 11, 2, 12, tzinfo=ZoneInfo(Z)), now, Z)
    assert mon.startswith("Next Monday")


def test_weekday_names_locale_independent(monkeypatch):
    import locale
    old = locale.setlocale(locale.LC_TIME)
    for loc in ("sv_SE.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, loc)
            break
        except locale.Error:
            continue
    try:
        from zoneinfo import ZoneInfo
        r = datetime(2026, 10, 30, 12, tzinfo=ZoneInfo(Z))
        assert reset_phrase(r, NOW, Z).startswith("Friday at")
    finally:
        locale.setlocale(locale.LC_TIME, old)


def test_exceeded_variants():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    both = quota_exceeded_reply(st(1.0, 5.0, "1.5", "6", now), now, Z)
    assert "Your Daily and Weekly spending limits have been reached:" in both
    wk = quota_exceeded_reply(st(1.0, 5.0, "0.5", "6", now), now, Z)
    assert "Your Weekly spending limit has been reached:" in wk
    assert quota_exceeded_reply(st(0.0, 5.0, "0", "6", now), now, Z) == blocked_reply()
    fb = "⚠️ Discord Quota Exceeded\n\nRun /quota to check your status."
    assert quota_exceeded_reply(st(1.0, 5.0, "0", "0", now), now, Z) == fb
    off = evaluate("u", QuotaConfig(enabled=False, default=Limits(1.0, 5.0)), L("9", "9"), now)
    assert quota_exceeded_reply(off, now, Z) == fb


def test_usd_negative_zero():
    from discord_quota.messages import usd
    assert usd(Decimal("-0.004")) == "$0.00"
    assert usd(Decimal("-0.50")) == "-$0.50"


def test_status_labels_and_lines():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    t = quota_status_text(st(0.0, 5.0, "0", "1", now), now, Z)
    assert "none, access blocked ($0.00 spent" in t and t.endswith("Status: Blocked")
    t = quota_status_text(st(1.0, 5.0, "1.2", "1", now), now, Z)
    assert "$0.00 / $1.00 remaining (120% used" in t and t.endswith("Status: Exceeded")
    assert quota_status_text(st(1.0, 5.0, "0.95", "1", now), now, Z).endswith("Status: Warning")
    off = evaluate("u", QuotaConfig(enabled=False, default=Limits(1.0, 5.0)), L("9", "9"), now)
    assert quota_status_text(off, now, Z).endswith("Status: Disabled")


def test_footer_choices():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    f = warning_footer(st(1.0, 5.0, "0.91", "4.8", now), now, Z)
    assert "96% of your Weekly" in f
    f = warning_footer(st(1.0, 5.0, "0.95", "4.75", now), now, Z)  # tie: daily first
    assert "95% of your Daily" in f
    f = warning_footer(st(1.0, 5.0, "1.5", "5.5", now), now, Z)
    assert "Daily spend is $1.50" in f and "until Friday at 12:00" in f


def test_reset_phrase_dst_and_naive():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(Z)
    r = datetime(2026, 10, 26, 12, tzinfo=tz)  # day after DST ends (10-25)
    # Naive now is UTC: 23:30Z on the 25th is 00:30 local on the 26th -> Today.
    assert reset_phrase(r, datetime(2026, 10, 25, 23, 30), Z) == "Today at 12:00 Europe/Stockholm"
    # 21:30Z on the 25th is 22:30 local on the 25th -> Tomorrow.
    assert reset_phrase(r, datetime(2026, 10, 25, 21, 30), Z) == "Tomorrow at 12:00 Europe/Stockholm"
    # Reset across the DST change still renders 12:00 local.
    r2 = datetime(2026, 10, 25, 12, tzinfo=tz)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert reset_phrase(r2, now, Z) == "Tomorrow at 12:00 Europe/Stockholm"
