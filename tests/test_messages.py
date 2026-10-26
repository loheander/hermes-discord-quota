from datetime import datetime, timezone
from decimal import Decimal

from discord_quota.config import Limits, QuotaConfig
from discord_quota.messages import (append_footer, blocked_reply, quota_exceeded_reply,
                                    quota_status_text, reset_phrase, usd, warning_footer)
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
    assert reset_phrase(mk(2026, 10, 26), mk(2026, 10, 26), Z) == "idag kl. 12:00"
    assert reset_phrase(mk(2026, 10, 27), NOW, Z) == "imorgon kl. 12:00"
    assert reset_phrase(mk(2026, 10, 30), NOW, Z) == "fredag kl. 12:00"
    assert reset_phrase(mk(2026, 11, 2), NOW, Z) == "nästa måndag kl. 12:00"


def test_every_weekday_and_next():
    names = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
    zi = __import__("zoneinfo").ZoneInfo(Z)
    now = datetime(2026, 10, 26, 8, 0, tzinfo=zi)  # Monday
    from datetime import timedelta
    for i, name in enumerate(names[2:], start=2):  # +2 .. +6 days
        r = datetime(2026, 10, 26, 12, tzinfo=zi) + timedelta(days=i)
        assert reset_phrase(r, now, Z) == f"{name} kl. 12:00"
    for i, name in enumerate(names):  # +7 .. +13 days
        r = datetime(2026, 11, 2, 12, tzinfo=zi) + timedelta(days=i)
        assert reset_phrase(r, now, Z) == f"nästa {name} kl. 12:00"


def test_usd_decimal_comma():
    assert usd(Decimal("0.45")) == "0,45 USD"
    assert usd(Decimal("0.005")) == "0,01 USD"
    assert usd(Decimal("-0.001")) == "0,00 USD"
    assert usd(Decimal("-1.5")) == "-1,50 USD"


def test_exceeded_reply_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)  # 09:00 local, before noon
    s = st(1.0, 5.0, "1.04", "3.18", now)
    assert quota_exceeded_reply(s, now, Z) == (
        "⚠️ Kvoten är slut\n\n"
        "Din dagliga kostnadsgräns är nådd:\n"
        "• Dagens kostnad: 1,04 USD / 1,00 USD (104%) — återställs idag kl. 12:00\n"
        "• Veckans kostnad: 3,18 USD / 5,00 USD (64%) — återställs fredag kl. 12:00\n\n"
        "Nya förfrågningar pausas till nästa återställning. Kör /quota för att se din status.")


def test_exceeded_both_windows():
    s = st(1.0, 5.0, "1.04", "5.5")
    assert "Dina dagliga och veckovisa kostnadsgränser är nådda:" in quota_exceeded_reply(s, NOW, Z)


def test_blocked():
    s = st(0.0, 5.0, "0", "0")
    assert quota_exceeded_reply(s, NOW, Z) == blocked_reply()
    assert blocked_reply().startswith("🚫 Åtkomst blockerad\n\nDu har ingen aktiv")


def test_status_text_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    s = st(1.0, 5.0, "0.42", "1.85", now)
    assert quota_status_text(s, now, Z) == (
        "📊 Kvotstatus för Discord\n\n"
        "• Dagskvot:  0,58 USD / 1,00 USD kvar (42% använt — återställs idag kl. 12:00)\n"
        "• Veckokvot: 3,15 USD / 5,00 USD kvar (37% använt — återställs fredag kl. 12:00)\n"
        "• Status: Aktiv")


def test_unlimited_shown():
    s = st(-1, -1, "0.5", "0.5")
    t = quota_status_text(s, NOW, Z)
    assert "obegränsad (0,50 USD förbrukat" in t and "Status: Aktiv" in t
    assert warning_footer(s, NOW, Z) is None


def test_footers():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert warning_footer(st(1.0, 5.0, "0.92", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Kvotvarning: Du har använt 92% av din dagliga budget (0,92 USD / 1,00 USD). "
        "Återställs kl. 12:00.")
    assert warning_footer(st(1.0, 5.0, "1.04", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Kvoten är slut: Dagens kostnad är 1,04 USD / 1,00 USD (104%). "
        "Nya förfrågningar pausas till idag kl. 12:00.")
    assert warning_footer(st(1.0, 5.0, "0.1", "1", now), now, Z) is None
    assert warning_footer(st(1.0, 5.0, "0.1", "4.6", now), now, Z).endswith(
        "av din veckovisa budget (4,60 USD / 5,00 USD). Återställs fredag kl. 12:00.")
