import sqlite3
import threading

import pytest
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
    assert not (tmp_path / "prof").exists()


def test_reset_window_credit_clamped_inside_window(tmp_path):
    l = mk(tmp_path)
    rec(l, 3, 100)
    assert l.reset_window("u1", 0, 200, timestamp=500) == Decimal("3")
    assert l.spend_since("u1", 0, 200) == Decimal("0")
    assert l.recent("u1", 1)[0].timestamp < 200
    rec(l, 1, 1000)
    l.reset_window("u1", 900, 2000, timestamp=10)  # before start: clamped up
    assert l.spend_since("u1", 900, 2000) == Decimal("0")


def test_reset_window_nothing_to_credit(tmp_path):
    l = mk(tmp_path)
    assert l.reset_window("u1", 0) == Decimal("0")
    assert l.recent("u1") == []


def test_reset_window_atomic_against_other_process_writer(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    db = tmp_path / "q.db"
    n = 300
    code = ("import sys\nsys.path.insert(0, %r)\n" % str(root) +
            "from discord_quota.ledger import Ledger\nl=Ledger(sys.argv[1])\n"
            "for _ in range(%d): l.record('u1','s','0.01','m','p',timestamp=100)" % n)
    l = Ledger(db)
    rec(l, 0, 100)
    p = subprocess.Popen([sys.executable, "-I", "-c", code, str(db)], cwd=root)
    credits = Decimal(0)
    for _ in range(100000):  # bounded; ends when the writer exits
        credits += l.reset_window("u1", 0, timestamp=100)
        if p.poll() is not None:
            break
    assert p.wait(timeout=30) == 0
    credits += l.reset_window("u1", 0, timestamp=100)
    assert l.spend_since("u1", 0) == Decimal(0)
    # Conservation: everything recorded was credited back.
    assert credits == Decimal("0.01") * n
    # Atomicity: each credit row zeroes the running sum of all rows inserted before it,
    # which fails if a record() could slip in between the sum and the credit insert.
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, source_kind, CAST(ROUND(cost_usd*1000000) AS INTEGER)"
                        " FROM quota_transactions ORDER BY id").fetchall()
    running = 0
    for _id, kind, micros in rows:
        running += micros
        if kind == "admin_reset":
            assert running == 0, f"credit row {_id} left residual {running}"


def test_transaction_propagates_original_error(tmp_path):
    from discord_quota.ledger import _transaction
    l = mk(tmp_path)
    conn = l._connect()

    class Err(Exception):
        pass

    with pytest.raises(Err):
        with _transaction(conn):
            conn.execute("COMMIT")  # transaction already gone; rollback must not mask Err
            raise Err()
    assert not conn.in_transaction

    class FailCommit:
        in_transaction = True

        def execute(self, sql, *a):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("commit boom")
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("cannot rollback")

    with pytest.raises(sqlite3.OperationalError, match="commit boom"):
        with _transaction(FailCommit()):
            pass
    conn.close()


def test_invalid_cost_recorded_as_zero(tmp_path, caplog):
    l = mk(tmp_path)
    for bad in (float("nan"), float("inf"), "abc", None):
        rec(l, bad, 100)
    assert l.spend_since("u1", 0) == Decimal("0")
    assert len(l.recent("u1", 10)) == 4
    assert "invalid cost" in caplog.text


def test_connect_failure_closes_connection(tmp_path, monkeypatch):
    l = mk(tmp_path)
    closed = []

    class Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            closed.append(1)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Boom())
    with pytest.raises(sqlite3.OperationalError):
        l.spend_since("u1", 0)
    assert closed == [1]


def test_fresh_interpreter_import_creates_nothing(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    code = ("import sys; sys.path.insert(0, %r)\n" % str(root) + "import discord_quota.ledger as m; m.Ledger(); "
            "from pathlib import Path\n"
            "try:\n m.default_db_path()\nexcept ImportError: pass")
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, capture_output=True,
                       text=True, env={**os.environ, "HERMES_HOME": str(home),
                                       "PYTHONPATH": str(root)})
    assert r.returncode == 0, r.stderr
    assert not home.exists() and list(tmp_path.iterdir()) == []


def test_second_process_writer(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    db = tmp_path / "q.db"
    code = ("import sys\nsys.path.insert(0, %r)\n" % str(root) + "from discord_quota.ledger import Ledger\nl=Ledger(sys.argv[1])\n"
            "for _ in range(50): l.record('u1','s','0.01','m','p',timestamp=100)")
    l = Ledger(db)
    rec(l, 0, 100)
    p = subprocess.Popen([sys.executable, "-I", "-c", code, str(db)], cwd=root,
                         env={**os.environ, "PYTHONPATH": str(root)})
    for _ in range(50):
        rec(l, "0.01", 100)
        l.spend_since("u1", 0)
    assert p.wait(timeout=30) == 0
    assert l.spend_since("u1", 0) == Decimal("1.00")
    assert len(l.recent("u1", 1000)) == 101


def test_fractional_sums_and_overcredit(tmp_path):
    l = mk(tmp_path)
    rec(l, 0.1, 100)
    rec(l, 0.2, 100)
    rec(l, -0.0000005, 100)
    assert l.spend_since("u1", 0) == Decimal("0.299999")
    l2 = mk(tmp_path)
    l2.add_credit("u1", "5", timestamp=101)
    assert l2.spend_since("u1", 0) == Decimal("-4.700001")


def test_delete_window_with_end_leaves_others(tmp_path):
    l = mk(tmp_path)
    for ts in (50, 150, 250):
        rec(l, 1, ts)
        rec(l, 1, ts, user="o")
    assert l.delete_window("u1", 100, 200) == 1
    assert l.spend_since("u1", 0) == Decimal("2")
    assert l.spend_since("o", 0) == Decimal("3")


def test_has_session_exclude_media(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record("1", "child", 0.1, "m", "p", source_kind="media")
    assert led.has_session("child")
    assert not led.has_session("child", exclude_media=True)
    led.record("1", "child", 0.1, "m", "p")
    assert led.has_session("child", exclude_media=True)
