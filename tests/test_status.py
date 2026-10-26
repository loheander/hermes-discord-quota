from datetime import datetime, timezone
from decimal import Decimal

import pytest

from discord_quota.config import Limits, QuotaConfig
from discord_quota.ledger import Ledger
from discord_quota.status import WindowState as S, evaluate

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)  # Monday


def cfg(daily, weekly, **kw):
    return QuotaConfig(enabled=True, default=Limits(daily, weekly), **kw)


class FakeLedger:
    def __init__(self, day="0", week="0"):
        self.day, self.week, self.calls = Decimal(day), Decimal(week), []

    def spend_since(self, user, start, end=None):
        self.calls.append((user, start, end))
        return self.day if len(self.calls) % 2 == 1 else self.week


def ev(daily, weekly, day="0", week="0", **kw):
    return evaluate("u1", cfg(daily, weekly, **kw), FakeLedger(day, week), NOW)


def test_below_threshold_active():
    st = ev(1.0, 10.0, day="0.89", week="1")
    assert st.daily.state is S.ACTIVE and st.weekly.state is S.ACTIVE
    assert st.admitted and not st.warn
    assert st.daily.remaining == Decimal("0.11")
    assert st.daily.percent == Decimal("89")


def test_warn_at_threshold_boundary():
    st = ev(1.0, 10.0, day="0.90")
    assert st.daily.state is S.WARN and st.admitted and st.warn


def test_exhausted_at_100():
    st = ev(1.0, 10.0, day="1.00")
    assert st.daily.state is S.EXHAUSTED and not st.admitted and not st.warn
    assert st.daily.remaining == 0


def test_overspent_clamps_remaining():
    st = ev(1.0, 10.0, day="2.5")
    assert st.daily.remaining == 0 and st.daily.percent == Decimal("250")


def test_unlimited_never_warns_or_blocks():
    st = ev(-1, -1, day="999", week="999")
    assert st.daily.state is S.UNLIMITED and st.weekly.state is S.UNLIMITED
    assert st.admitted and not st.warn
    assert st.daily.remaining is None and st.daily.percent is None


def test_blocked_distinct_from_exhausted():
    st = ev(0, 10.0)
    assert st.daily.state is S.BLOCKED and not st.admitted
    assert st.daily.state is not S.EXHAUSTED
    assert st.daily.remaining == 0 and st.daily.percent is None
    assert st.weekly.state is S.ACTIVE


def test_mixed_daily_ok_weekly_exhausted():
    st = ev(1.0, 5.0, day="0.1", week="5")
    assert st.daily.state is S.ACTIVE and st.weekly.state is S.EXHAUSTED
    assert not st.admitted


def test_mixed_daily_warn_weekly_unlimited():
    st = ev(1.0, -1, day="0.95", week="50")
    assert st.admitted and st.warn
    assert st.daily.warn and not st.weekly.warn


def test_negative_spend_after_credit():
    st = ev(1.0, 10.0, day="-0.5")
    assert st.daily.state is S.ACTIVE and st.daily.percent == 0
    assert st.daily.remaining == Decimal("1.5")


def test_custom_threshold():
    st = ev(1.0, 10.0, day="0.5", warn_threshold=0.5)
    assert st.daily.state is S.WARN


def test_disabled_admits():
    st = evaluate("u1", QuotaConfig(enabled=False), FakeLedger("5", "5"), NOW)
    assert st.admitted and not st.warn


def test_windows_and_user_passed_to_ledger():
    led = FakeLedger()
    st = evaluate(42, cfg(1.0, 1.0), led, NOW)
    assert led.calls[0][0] == "42"
    assert led.calls[0][1] == st.daily.start and led.calls[0][2] == st.daily.resets_at
    assert led.calls[1][1] == st.weekly.start
    assert st.daily.resets_at > NOW >= st.daily.start


def test_per_user_override():
    c = QuotaConfig(enabled=True, default=Limits(1, 1), users={"7": Limits(-1, -1)})
    assert evaluate("7", c, FakeLedger("9", "9"), NOW).admitted
    assert not evaluate("8", c, FakeLedger("9", "9"), NOW).admitted


def test_now_defaults_to_current_time():
    assert evaluate("u", cfg(1.0, 1.0), FakeLedger()).admitted


def test_with_real_ledger(tmp_path):
    led = Ledger(tmp_path / "q.db")
    led.record("u1", "s", 0.95, "m", "p", timestamp=NOW.timestamp())
    led.record("u1", "s", 9.0, "m", "p", timestamp=NOW.timestamp() + 86400 * 30)
    st = evaluate("u1", cfg(1.0, 1.0), led, NOW)
    assert st.daily.spent == Decimal("0.95") and st.warn and st.admitted
