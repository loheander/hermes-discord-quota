from decimal import Decimal

from discord_quota.ledger import Ledger
from discord_quota.subagent import SubagentAccountant


class R:
    def __init__(self, user="42"):
        self.user, self.calls = user, []

    def resolve(self, session_id=None, turn_id=None, session_key=None):
        self.calls.append((session_id, turn_id))
        return self.user


def make(tmp_path, user="42", cost=Decimal("0.5")):
    led = Ledger(tmp_path / "q.db")
    looked = []

    def lookup(cid):
        looked.append(cid)
        return cost, "m", "p"
    return led, SubagentAccountant(led, R(user), lookup), looked


KW = dict(parent_session_id="p1", parent_turn_id="t1", child_session_id="c1")


def test_records_once_even_if_fired_twice(tmp_path):
    led, acc, _ = make(tmp_path)
    acc.subagent_stop(**KW)
    acc.subagent_stop(**KW)
    rows = led.recent("42")
    assert len(rows) == 1
    assert rows[0].source_kind == "subagent" and rows[0].session_id == "c1"
    assert rows[0].cost_usd == Decimal("0.5")


def test_already_recorded_per_call_skipped(tmp_path):
    led, acc, looked = make(tmp_path)
    led.record("42", "c1", "0.2", "m", "p", turn_id="t1")
    acc.subagent_stop(**KW)
    assert len(led.recent("42")) == 1 and looked == []


def test_non_discord_ignored(tmp_path):
    led, acc, looked = make(tmp_path, user=None)
    acc.subagent_stop(**KW)
    assert led.recent("42") == [] and looked == []


def test_zero_cost_not_recorded(tmp_path):
    led, acc, _ = make(tmp_path, cost=None)
    acc.subagent_stop(**KW)
    assert led.recent("42") == []


def test_errors_never_raise(tmp_path):
    led = Ledger(tmp_path / "q.db")

    def boom(_):
        raise RuntimeError("x")
    SubagentAccountant(led, R(), boom).subagent_stop(**KW)
    SubagentAccountant(led, R(), boom).subagent_stop()


def test_missing_child_ignored(tmp_path):
    led, acc, _ = make(tmp_path)
    acc.subagent_stop(parent_session_id="p1")
    assert led.recent("42") == []
