from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from discord_quota.windows import daily_window, weekly_window

STO = ZoneInfo("Europe/Stockholm")


def L(*a):
    return datetime(*a, tzinfo=STO)


def test_daily_boundary():
    assert daily_window(L(2026, 1, 14, 11, 59), STO) == (L(2026, 1, 13, 12), L(2026, 1, 14, 12))
    assert daily_window(L(2026, 1, 14, 12, 0), STO) == (L(2026, 1, 14, 12), L(2026, 1, 15, 12))
    assert daily_window(L(2026, 1, 14, 12, 1), STO)[0] == L(2026, 1, 14, 12)


@pytest.mark.parametrize("day", range(5, 12))  # 2026-01-05 Mon .. 01-11 Sun
def test_weekly_every_weekday(day):
    # 2026-01-09 is a Friday
    before = weekly_window(L(2026, 1, day, 11, 59), STO)
    after = weekly_window(L(2026, 1, day, 12, 0), STO)
    wd = L(2026, 1, day).weekday()
    if wd == 4:
        assert before == (L(2026, 1, 2, 12), L(2026, 1, 9, 12))
        assert after == (L(2026, 1, 9, 12), L(2026, 1, 16, 12))
    elif wd < 4:
        assert before == after == (L(2026, 1, 2, 12), L(2026, 1, 9, 12))
    else:
        assert before == after == (L(2026, 1, 9, 12), L(2026, 1, 16, 12))


def test_dst_spring_daily_keeps_noon():
    # CET->CEST on 2026-03-29
    start, nxt = daily_window(L(2026, 3, 28, 13), STO)
    assert (start, nxt) == (L(2026, 3, 28, 12), L(2026, 3, 29, 12))
    assert nxt.utcoffset().total_seconds() == 7200 and start.utcoffset().total_seconds() == 3600
    assert (nxt.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 23 * 3600


def test_dst_autumn_daily_keeps_noon():
    # CEST->CET on 2026-10-25
    start, nxt = daily_window(L(2026, 10, 24, 13), STO)
    assert nxt == L(2026, 10, 25, 12)
    assert (nxt.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 25 * 3600


def test_dst_weekly():
    s, n = weekly_window(L(2026, 3, 27, 12), STO)  # Friday before spring change
    assert (s, n) == (L(2026, 3, 27, 12), L(2026, 4, 3, 12))
    assert (n.astimezone(timezone.utc) - s.astimezone(timezone.utc)).total_seconds() == 7 * 24 * 3600 - 3600
    s, n = weekly_window(L(2026, 10, 23, 12), STO)
    assert n == L(2026, 10, 30, 12)
    assert (n.astimezone(timezone.utc) - s.astimezone(timezone.utc)).total_seconds() == 7 * 24 * 3600 + 3600


def test_utc_and_naive_inputs_converted():
    utc = datetime(2026, 1, 14, 11, 30, tzinfo=timezone.utc)  # 12:30 Stockholm
    assert daily_window(utc, STO)[0] == L(2026, 1, 14, 12)
    assert daily_window(utc.replace(tzinfo=None), "Europe/Stockholm")[0] == L(2026, 1, 14, 12)
    summer = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)  # 12:30 CEST
    assert daily_window(summer, STO)[0] == L(2026, 7, 1, 12)
    early = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)  # 11:30 CEST
    assert daily_window(early, STO)[0] == L(2026, 6, 30, 12)
    assert weekly_window(utc, STO)[0].tzinfo is STO
