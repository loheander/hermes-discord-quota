import argparse
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from discord_quota.cli import AdminCli, parse_amount
from discord_quota.config import Limits, QuotaConfig
from discord_quota.ledger import Ledger
from discord_quota.windows import daily_window, weekly_window

# Monday 2026-10-26 15:00 UTC = 16:00 CET; daily window started 12:00 same day,
# weekly window started Friday 2026-10-23 12:00.
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
# Saturday: daily and weekly windows start... Friday 12:00 local, so use Friday 15:00 local.
FRI = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)


def build(tmp_path, now=NOW, limits=Limits(1.0, 5.0)):
    cfg = QuotaConfig(enabled=True, default=limits)
    ledger = Ledger(tmp_path / "q.db")
    out, err = io.StringIO(), io.StringIO()
    cli = AdminCli(lambda: cfg, ledger, clock=lambda: now, out=out, err=err)
    return cli, ledger, cfg, out, err


def spend(ledger, cfg, now, user="42"):
    d = daily_window(now, cfg.timezone)
    w = weekly_window(now, cfg.timezone)
    return ledger.spend_since(user, *d), ledger.spend_since(user, *w)


def seed(ledger, now):
    w_start, _ = weekly_window(now, "Europe/Stockholm")
    d_start, _ = daily_window(now, "Europe/Stockholm")
    ledger.record("42", "s", "0.50", "m", "p", timestamp=w_start + timedelta(hours=1))  # weekly only (if differs)
    ledger.record("42", "s", "0.30", "m", "p", timestamp=d_start + timedelta(minutes=5))
    ledger.record("42", "s", "0.20", "m", "p", timestamp=now)


def test_reset_both_default(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path)
    seed(ledger, NOW)
    assert spend(ledger, cfg, NOW) == (Decimal("0.5"), Decimal("1.0"))
    assert cli.reset("42") == 0
    assert spend(ledger, cfg, NOW) == (Decimal(0), Decimal(0))
    assert "Weekly window reset" in out.getvalue() and "Daily window reset" in out.getvalue()


def test_reset_daily_only_also_lowers_weekly(tmp_path):
    cli, ledger, cfg, *_ = build(tmp_path)
    seed(ledger, NOW)
    cli.reset("42", daily=True)
    assert spend(ledger, cfg, NOW) == (Decimal(0), Decimal("0.5"))


def test_reset_weekly_only_leaves_daily_untouched(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path)
    seed(ledger, NOW)
    cli.reset("42", weekly=True)
    assert spend(ledger, cfg, NOW) == (Decimal("0.5"), Decimal(0))
    assert "reduced too" not in out.getvalue()


def test_reset_weekly_when_daily_shares_start_zeroes_daily_explicitly(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path, now=FRI)
    d, w = daily_window(FRI, cfg.timezone), weekly_window(FRI, cfg.timezone)
    assert d[0] == w[0]
    ledger.record("42", "s", "0.40", "m", "p", timestamp=FRI)
    cli.reset("42", weekly=True)
    assert spend(ledger, cfg, FRI) == (Decimal(0), Decimal(0))
    assert "daily window starts with the weekly window" in out.getvalue()


def test_reset_is_append_only_and_idempotent(tmp_path):
    cli, ledger, cfg, *_ = build(tmp_path)
    seed(ledger, NOW)
    cli.reset("42")
    rows = ledger.recent("42", 50)
    assert {t.source_kind for t in rows} >= {"admin_reset", "api"}
    n = len(rows)
    cli.reset("42")
    assert len(ledger.recent("42", 50)) == n  # nothing to offset: no new rows
    assert sum((t.cost_usd for t in rows if t.source_kind == "api"), Decimal(0)) == Decimal("1.0")


def test_reset_other_user_untouched(tmp_path):
    cli, ledger, cfg, *_ = build(tmp_path)
    seed(ledger, NOW)
    ledger.record("7", "s", "0.25", "m", "p", timestamp=NOW)
    cli.reset("42")
    assert spend(ledger, cfg, NOW, "7") == (Decimal("0.25"), Decimal("0.25"))


def test_reset_empty_user(tmp_path):
    cli, *_rest, err = build(tmp_path)
    assert cli.reset("  ") == 2 and "user_id" in err.getvalue()


def test_credit(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path)
    ledger.record("42", "s", "0.90", "m", "p", timestamp=NOW)
    assert cli.credit("42", "0.25") == 0
    assert spend(ledger, cfg, NOW)[0] == Decimal("0.65")
    assert ledger.recent("42", 1)[0].source_kind == "admin_credit"
    assert "Credited $0.250000 to 42." in out.getvalue()


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "nan", "inf", "-inf", "0.0000001", "1e999999"])
def test_credit_rejects_bad_amounts(tmp_path, bad):
    cli, ledger, cfg, out, err = build(tmp_path)
    assert cli.credit("42", bad) == 2
    assert "error:" in err.getvalue()
    assert ledger.recent("42") == []


def test_parse_amount():
    assert parse_amount(" 1.5 ") == Decimal("1.500000")
    with pytest.raises(ValueError):
        parse_amount(None)


def test_status_user_deterministic(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path)
    ledger.record("42", "s", "0.40", "gpt", "openai", timestamp=NOW - timedelta(hours=1))
    cli.status("42")
    first = out.getvalue()
    assert first.startswith("User 42  (ok)\nWindows:\n")
    assert "Daily   spent $0.400000  limit $1.000000  state active" in first
    assert "Weekly  spent $0.400000  limit $5.000000  state active" in first
    assert "resets 2026-10-27 12:00 Europe/Stockholm" in first
    assert "$0.400000  api  openai/gpt" in first
    out.truncate(0); out.seek(0)
    cli.status("42")
    assert out.getvalue() == first


def test_status_user_without_rows(tmp_path):
    cli, _l, _c, out, _ = build(tmp_path)
    cli.status("99")
    assert "  none" in out.getvalue()


def test_status_all_users(tmp_path):
    cli, ledger, cfg, out, _ = build(tmp_path)
    ledger.record("b", "s", "1.5", "m", "p", timestamp=NOW)
    ledger.record("a", "s", "0.1", "m", "p", timestamp=NOW)
    cli.status()
    assert out.getvalue() == (
        "Discord quota status (2 users)\n"
        "a  daily $0.100000 / $1.000000  weekly $0.100000 / $5.000000  ok\n"
        "b  daily $1.500000 / $1.000000  weekly $1.500000 / $5.000000  exhausted\n")


def test_status_all_users_empty(tmp_path):
    cli, _l, _c, out, _ = build(tmp_path)
    cli.status()
    assert "No quota transactions" in out.getvalue()


def test_argparse_wiring_and_run(tmp_path):
    cli, ledger, cfg, out, err = build(tmp_path)
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    ledger.record("42", "s", "0.40", "m", "p", timestamp=NOW)
    assert cli.run(parser.parse_args(["reset", "42", "--daily"])) == 0
    assert spend(ledger, cfg, NOW)[0] == 0
    assert cli.run(parser.parse_args(["credit", "42", "-3"])) == 2
    assert cli.run(parser.parse_args(["status"])) == 0
    assert cli.run(parser.parse_args([])) == 2


def test_run_reports_errors(tmp_path):
    cli, ledger, *_ , err = build(tmp_path)
    ledger.user_ids = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert cli.run(argparse.Namespace(quota_action="status", user_id=None)) == 1
    assert "db down" in err.getvalue()


def test_status_blank_user_id_is_error(tmp_path):
    cli, ledger, cfg, out, err = build(tmp_path)
    assert cli.status("  ") == 2
    assert "empty" in err.getvalue()


def test_registered(fake_ctx):
    from discord_quota import register
    register(fake_ctx)
    cmd = [c for c in fake_ctx.cli_commands if c["name"] == "quota"]
    assert len(cmd) == 1 and callable(cmd[0]["setup_fn"]) and callable(cmd[0]["handler_fn"])
