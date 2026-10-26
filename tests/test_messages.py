from datetime import datetime, timezone
from decimal import Decimal

from discord_quota.config import Limits, QuotaConfig
from discord_quota.messages import (append_footer, blocked_reply, concurrent_reply,
                                    quota_exceeded_reply, quota_status_text, reset_phrase,
                                    unavailable_reply, usd, warning_footer)
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


def test_usd_shown_as_kronor():
    assert usd(Decimal("0.45")) == "4,50 kr"
    assert usd(Decimal("1.00")) == "10,00 kr"
    assert usd(Decimal("0.0005")) == "0,01 kr"
    assert usd(Decimal("0.00049")) == "0,00 kr"
    assert usd(Decimal("-0.0001")) == "0,00 kr"
    assert usd(Decimal("-1.5")) == "-15,00 kr"


def test_unavailable_reply_exact():
    assert unavailable_reply() == "Kan inte kolla hur mycket pengar du har kvar, försök senare."


def test_concurrent_reply():
    assert concurrent_reply(Decimal("0.05")) == (
        "Du har en fråga som fortfarande pågår och din återstående budget är låg "
        "(0,50 kr). Vänta tills den är klar och försök igen.")


def test_exceeded_reply_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)  # 09:00 local, before noon
    s = st(1.0, 5.0, "1.04", "3.18", now)
    assert quota_exceeded_reply(s, now, Z) == (
        "⚠️ Budgeten är slut\n\n"
        "Du har använt upp din budget per dag:\n"
        "• Per dag: 10,40 kr av 10,00 kr (104%) — återställs idag kl. 12:00\n"
        "• Per vecka: 31,80 kr av 50,00 kr (64%) — återställs fredag kl. 12:00\n\n"
        "Du kan inte skriva till boten förrän det återställs. Skriv /quota för att se hur mycket du har kvar.")


def test_exceeded_both_windows():
    s = st(1.0, 5.0, "1.04", "5.5")
    assert "Du har använt upp din budget per dag och per vecka:" in quota_exceeded_reply(s, NOW, Z)


def test_blocked():
    s = st(0.0, 5.0, "0", "0")
    assert quota_exceeded_reply(s, NOW, Z) == blocked_reply()
    assert blocked_reply() == (
        "🚫 Ingen åtkomst\n\n"
        "Du har ingen budget för den här boten.\n"
        "Fråga den som driver boten om du kan få använda den.")


def test_status_text_snapshot():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    s = st(1.0, 5.0, "0.42", "1.85", now)
    assert quota_status_text(s, now, Z) == (
        "📊 Din budget\n\n"
        "• Per dag: 5,80 kr kvar av 10,00 kr (42% använt — återställs idag kl. 12:00)\n"
        "• Per vecka: 31,50 kr kvar av 50,00 kr (37% använt — återställs fredag kl. 12:00)\n"
        "• Läge: Ok")


def test_unlimited_shown():
    s = st(-1, -1, "0.5", "0.5")
    t = quota_status_text(s, NOW, Z)
    assert "ingen gräns (5,00 kr använt" in t and "Läge: Ok" in t
    assert warning_footer(s, NOW, Z) is None


def test_footers():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert warning_footer(st(1.0, 5.0, "0.92", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Budgetvarning: Du har använt 92% av din budget per dag (9,20 kr av 10,00 kr). "
        "Återställs kl. 12:00.")
    assert warning_footer(st(1.0, 5.0, "1.04", "1", now), now, Z) == (
        f"{RULE}\n⚠️ Budgeten är slut: du har använt 10,40 kr av 10,00 kr per dag (104%). "
        "Du kan skriva igen idag kl. 12:00.")
    assert warning_footer(st(1.0, 5.0, "0.1", "1", now), now, Z) is None
    assert warning_footer(st(1.0, 5.0, "0.1", "4.6", now), now, Z).endswith(
        "per vecka (46,00 kr av 50,00 kr). Återställs fredag kl. 12:00.")


def test_no_usd_or_old_words_in_any_text():
    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    texts = [
        blocked_reply(), unavailable_reply(), concurrent_reply(Decimal("0.05")),
        quota_exceeded_reply(st(1.0, 5.0, "1.04", "5.5", now), now, Z),
        quota_exceeded_reply(st(1.0, 5.0, "0.1", "0.1", now), now, Z),
        quota_status_text(st(1.0, 5.0, "0.42", "1.85", now), now, Z),
        quota_status_text(st(-1, -1, "0.5", "0.5", now), now, Z),
        quota_status_text(st(0.0, 5.0, "0", "0", now), now, Z),
        warning_footer(st(1.0, 5.0, "0.92", "1", now), now, Z),
        warning_footer(st(1.0, 5.0, "1.04", "1", now), now, Z),
        warning_footer(st(1.0, 5.0, "0.1", "4.6", now), now, Z),
    ]
    for t in texts:
        for bad in ("USD", "$", "dagliga", "veckovisa", "Dagens", "Veckans", "kvot", "Kvot",
                    "förfrågan", "förfrågningar", "administratör", "fönster", "pengar att"):
            assert bad not in t, (bad, t)


def test_budget_wording_and_no_kvot_anywhere():
    from discord_quota import guard
    from discord_quota.command import DESCRIPTION, NOT_DISCORD_REPLY

    now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    exceeded = quota_exceeded_reply(st(1.0, 5.0, "1.04", "5.5", now), now, Z)
    generic = quota_exceeded_reply(st(1.0, 5.0, "0.1", "0.1", now), now, Z)
    exhausted_footer = warning_footer(st(1.0, 5.0, "1.04", "1", now), now, Z)
    warn_footer = warning_footer(st(1.0, 5.0, "0.92", "1", now), now, Z)
    assert exceeded.startswith("⚠️ Budgeten är slut")
    assert generic.startswith("⚠️ Budgeten är slut")
    assert exhausted_footer.split("\n", 1)[1].startswith("⚠️ Budgeten är slut")
    assert warn_footer.split("\n", 1)[1].startswith("⚠️ Budgetvarning: Du har använt")
    assert "din återstående budget är låg" in concurrent_reply(Decimal("0.05"))
    texts = [
        exceeded, generic, exhausted_footer, warn_footer, blocked_reply(), unavailable_reply(),
        concurrent_reply(Decimal("0.05")), DESCRIPTION, NOT_DISCORD_REPLY, guard.BLOCK_MESSAGE,
        quota_status_text(st(1.0, 5.0, "0.42", "1.85", now), now, Z),
        quota_status_text(st(0.0, 5.0, "0", "0", now), now, Z),
    ]
    for t in texts:
        assert "kvot" not in t.lower(), t
