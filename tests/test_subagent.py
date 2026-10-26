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


def test_after_record_called_after_record_and_errors_swallowed(tmp_path):
    led = Ledger(tmp_path / "q.db")
    seen = []

    def boom(*a):
        seen.append(a)
        raise RuntimeError("x")
    acc = SubagentAccountant(led, R(), lambda c: (Decimal("0.5"), "m", "p"), after_record=boom)
    acc.subagent_stop(**KW)
    assert seen == [("42", "p1", "t1", 1)] and len(led.recent("42")) == 1
    acc.subagent_stop(**KW)  # deduped: no second call
    assert len(seen) == 1


def _state_db(tmp_path, monkeypatch, rows):
    import sqlite3
    import sys
    import types
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,"
                 " model TEXT, billing_provider TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    mod = types.ModuleType("hermes_constants")
    mod.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)


def test_default_lookup_precedence_and_edge_cases(tmp_path, monkeypatch):
    from discord_quota.subagent import _default_cost_lookup as look
    nan = float("nan")
    _state_db(tmp_path, monkeypatch, [
        ("both", 0.3, 0.2, "m1", "p1"),
        ("est", 0.3, None, "m2", "p2"),
        ("zeroact", 0.3, 0.0, "m3", None),
        ("nan", nan, nan, None, None),
        ("none", None, None, "m", "p"),
    ])
    assert look("both") == (Decimal("0.2"), "m1", "p1")
    assert look("est") == (Decimal("0.3"), "m2", "p2")
    assert look("zeroact") == (Decimal("0.3"), "m3", "")
    assert look("nan")[0] is None
    assert look("none")[0] is None
    assert look("missing") == (None, "", "")


def test_default_lookup_missing_db_does_not_raise_through_hook(tmp_path, monkeypatch):
    import sys
    import types
    mod = types.ModuleType("hermes_constants")
    mod.get_hermes_home = lambda: tmp_path / "nope"
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    led = Ledger(tmp_path / "q.db")
    SubagentAccountant(led, R()).subagent_stop(**KW)
    assert led.recent("42") == []
