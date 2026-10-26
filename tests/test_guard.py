from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from discord_quota.config import Limits, QuotaConfig
from discord_quota.guard import BLOCK_MESSAGE, MidTurnGuard
from discord_quota.messages import unavailable_reply
from discord_quota.recorder import SpendRecorder
from discord_quota.resolver import UserResolver

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class Ledger:
    def __init__(self, spent="0", boom=False):
        self.spent, self.boom, self.rows = Decimal(spent), boom, []

    def spend_since(self, user, start, end=None):
        if self.boom:
            raise RuntimeError("db")
        return self.spent

    def record(self, user, session, cost, *a, **kw):
        self.rows.append(cost)


class Agent:
    session_id = "s1"

    def __init__(self):
        self.calls = []

    def interrupt(self, *a, **kw):
        self.calls.append((a, kw))


def make(daily=1.0, weekly=5.0, spent="2", enabled=True, user="42", ledger=None, interrupter=None,
         running=None):
    cfg = QuotaConfig(enabled=enabled, default=Limits(daily, weekly))
    resolver = UserResolver(context_getter=lambda: ("", ""))
    if user:
        resolver.bind_turn("t1", "discord", user)
    agent = Agent()
    gw = SimpleNamespace(_running_agents=running if running is not None else {"k": agent})
    calls = []

    def intr(a, message=None, *, tool_reason=None):
        calls.append((a, tool_reason))
        return True

    g = MidTurnGuard(lambda: cfg, ledger or Ledger(spent), resolver, gateway_getter=lambda: gw,
                     clock=lambda: NOW, interrupter=interrupter or intr,
                     session_key_getter=lambda: "k")
    return g, calls, agent


def test_blocks_when_exhausted_daily():
    g, calls, agent = make(spent="1")
    r = g.pre_tool_call(tool_name="x", session_id="s1", turn_id="t1")
    assert r == {"action": "block", "message": BLOCK_MESSAGE}
    assert g.is_exhausted("42")
    assert calls == [(agent, "quota exhausted")]


def test_blocks_weekly_only():
    g, _, _ = make(daily=-1, weekly=2.0, spent="2")
    assert g.pre_tool_call(session_id="s1", turn_id="t1")["action"] == "block"


def test_blocks_blocked_window():
    g, _, _ = make(daily=0, weekly=-1, spent="0")
    assert g.pre_tool_call(session_id="s1", turn_id="t1")["action"] == "block"


def test_under_limit_none():
    g, calls, _ = make(spent="0.5")
    assert g.pre_tool_call(session_id="s1", turn_id="t1") is None
    assert calls == []


def test_unlimited_never_blocked():
    g, _, _ = make(daily=-1, weekly=-1, spent="999")
    assert g.pre_tool_call(session_id="s1", turn_id="t1") is None


def test_non_discord_and_disabled():
    g, _, _ = make(user=None, spent="9")
    assert g.pre_tool_call(session_id="s1", turn_id="t1") is None
    g, _, _ = make(enabled=False, spent="9")
    assert g.pre_tool_call(session_id="s1", turn_id="t1") is None


def test_interrupt_once_per_turn():
    g, calls, _ = make(spent="1")
    for _ in range(3):
        g.pre_tool_call(session_id="s1", turn_id="t1")
    g.after_record("42", "s1", "t1", 2)
    assert len(calls) == 1


def test_interrupt_failure_logged_not_raised(caplog):
    def boom(*a, **k):
        raise RuntimeError("x")
    g, _, _ = make(spent="1", interrupter=boom)
    assert g.pre_tool_call(session_id="s1", turn_id="t1")["action"] == "block"
    assert "hard interrupt failed" in caplog.text


def test_no_running_agent_logged(caplog):
    g, calls, _ = make(spent="1", running={})
    assert g.pre_tool_call(session_id="s1", turn_id="t1")["action"] == "block"
    assert calls == []


def test_fail_closed_on_ledger_error():
    g, _, _ = make(ledger=Ledger(boom=True))
    assert g.pre_tool_call(session_id="s1", turn_id="t1") == {
        "action": "block", "message": unavailable_reply()}


def test_after_record_requires_tool_calls():
    g, calls, _ = make(spent="1")
    g.after_record("42", "s1", "t1", 0)
    assert calls == [] and g.is_exhausted("42")
    g.after_record("42", "s1", "t1", 1)
    assert len(calls) == 1


def test_marker_cleared_after_credit():
    g, _, _ = make(spent="1")
    g.after_record("42", "s1", "t1", 0)
    assert g.is_exhausted("42")
    g._ledger.spent = Decimal("0")
    assert g.pre_tool_call(session_id="s1", turn_id="t1") is None
    assert not g.is_exhausted("42")


def test_finds_agent_by_session_id_without_key():
    g, calls, agent = make(spent="1")
    g._session_key = lambda: ""
    g.after_record("42", "s1", "t1", 1)
    assert calls == [(agent, "quota exhausted")]


def test_recorder_triggers_guard():
    g, calls, _ = make(spent="1")
    led = Ledger("1")
    rec = SpendRecorder(led, g._resolver, lambda *a: (Decimal("0.1"), "estimated"),
                        after_record=g.after_record)
    rec.post_api_request(session_id="s1", turn_id="t1", model="m", provider="p", usage={},
                         assistant_tool_call_count=2)
    assert len(calls) == 1 and led.rows
