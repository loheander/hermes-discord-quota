import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal

from discord_quota.ledger import Ledger, round_cost


def mk(tmp_path):
    return Ledger(tmp_path / "q.db")


def rec(l, cost, ts, user="u1", **kw):
    return l.record(user, "s", cost, "m", "p", timestamp=ts, **kw)


def test_no_file_at_import_or_construction(tmp_path):
    mk(tmp_path)
    assert not (tmp_path / "q.db").exists()


def test_schema_and_pragmas(tmp_path):
    l = mk(tmp_path)
    rec(l, "0.1", 100)
    c = sqlite3.connect(l.path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cols = [r[1] for r in c.execute("PRAGMA table_info(quota_transactions)")]
    assert cols == ["id", "user_id", "session_id", "turn_id", "timestamp", "cost_usd", "model",
                    "provider", "source_kind", "input_tokens", "output_tokens", "created_at"]
    idx = c.execute("PRAGMA index_info(idx_trans_user_time)").fetchall()
    assert [r[2] for r in idx] == ["user_id", "timestamp"]


def test_rounding_and_decimal_sum(tmp_path):
    l = mk(tmp_path)
    assert round_cost(0.1234565) == Decimal("0.123457")
    rec(l, 0.0000004, 100)
    rec(l, 0.1234565, 100)
    rec(l, 0.1, 100)
    s = l.spend_since("u1", 0)
    assert isinstance(s, Decimal) and s == Decimal("0.223457")
    assert l.recent("u1", 5)[0].cost_usd == Decimal("0.1")


def test_window_start_inclusive_user_scoped_and_datetime(tmp_path):
    l = mk(tmp_path)
    rec(l, 1, 99)
    rec(l, 2, 100)
    rec(l, 4, 200, user="other")
    assert l.spend_since("u1", 100) == Decimal("2")
    start = datetime.fromtimestamp(100, timezone.utc)
    assert l.spend_since("u1", start) == Decimal("2")
    assert l.spend_since("u1", 100, 100) == Decimal("0")


def test_credit_reduces_and_reset(tmp_path):
    l = mk(tmp_path)
    rec(l, 3, 100)
    l.add_credit("u1", "1", timestamp=150)
    assert l.spend_since("u1", 0) == Decimal("2")
    assert l.reset_window("u1", 0, timestamp=160) == Decimal("2")
    assert l.spend_since("u1", 0) == Decimal("0")
    assert l.recent("u1", 1)[0].source_kind == "admin_reset"


def test_delete_window(tmp_path):
    l = mk(tmp_path)
    rec(l, 1, 50)
    rec(l, 1, 150)
    assert l.delete_window("u1", 100) == 1
    assert l.spend_since("u1", 0) == Decimal("1")


def test_recent_order_and_limit(tmp_path):
    l = mk(tmp_path)
    for i in range(5):
        rec(l, 1, 100 + i, turn_id=str(i))
    r = l.recent("u1", 3)
    assert [t.turn_id for t in r] == ["4", "3", "2"]


def test_concurrent_writes(tmp_path):
    l = mk(tmp_path)
    errors = []

    def work(n):
        try:
            for _ in range(25):
                rec(l, "0.01", 100, user=f"u{n % 2}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert l.spend_since("u0", 0) + l.spend_since("u1", 0) == Decimal("2.00")


def test_default_path_uses_hermes_home(tmp_path, monkeypatch):
    import sys, types
    mod = types.ModuleType("hermes_constants")
    mod.get_hermes_home = lambda: tmp_path / "prof"
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    l = Ledger()
    assert l.path == tmp_path / "prof" / "discord_quota.db"
