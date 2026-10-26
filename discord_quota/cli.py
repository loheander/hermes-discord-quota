"""``hermes quota status|reset|credit`` admin CLI (PLAN.md section 7.4).

Hermes contract (hermes_cli/main.py): ``setup_fn(subparser)`` configures the
argparse subparser and ``handler_fn(args) -> int`` is installed as
``func``. Output goes to stdout, errors to stderr; the return value is the
exit code.

Reset semantics (the ledger stays append-only): a reset inserts an offsetting
``admin_reset`` credit equal to the window's current spend, so the window reads
exactly 0 afterwards. Windows already at or below 0 get no row. Credits are
negative rows and reduce every window containing their timestamp:

* ``--weekly`` stamps its credit at the weekly window start, so the daily
  window is left untouched unless both windows start at the same instant
  (Friday 12:00 onwards). There the windows hold the same rows, so the weekly
  reset necessarily zeroes the daily window too; this is reported explicitly.
* ``--daily`` stamps its credit at now, which lowers the weekly sum as well
  (the spend really is removed).
* No flag (or both) resets daily first (which lowers the weekly sum), then
  weekly from a fresh sum, so both end at exactly 0.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

from .ledger import round_cost
from .status import QuotaStatus, WindowState, WindowStatus, evaluate
from .windows import daily_window, weekly_window

CLI_DESCRIPTION = (
    "Admin tools for the Discord spending quota:\n"
    "  hermes quota status [user_id]\n"
    "  hermes quota reset <user_id> [--daily] [--weekly]\n"
    "  hermes quota credit <user_id> <amount_usd>"
)
RECENT_N = 10


def parse_amount(text: Any) -> Decimal:
    """Parse a positive, finite USD amount (at least 0.000001 after rounding)."""
    try:
        value = Decimal(str(text).strip())
    except (ArithmeticError, ValueError):
        raise ValueError(f"amount must be a number, got {text!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"amount must be a positive number, got {text!r}")
    try:
        rounded = round_cost(value)
    except ArithmeticError:
        raise ValueError(f"amount is too large, got {text!r}") from None
    if rounded <= 0:
        raise ValueError(f"amount is too small (minimum 0.000001), got {text!r}")
    return rounded


def _money(v: Decimal) -> str:
    return f"${Decimal(v).quantize(Decimal('0.000001'))}"


def _limit(w: WindowStatus) -> str:
    if w.state is WindowState.UNLIMITED:
        return "unlimited"
    if w.state is WindowState.BLOCKED:
        return "blocked"
    return _money(w.limit)


def _fmt_ts(ts: float, zone: str) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S")


def _window_line(w: WindowStatus, zone: str) -> str:
    resets = w.resets_at.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")
    return (f"  {w.name.capitalize():<7} spent {_money(w.spent)}  limit {_limit(w)}  "
            f"state {w.state.value}  resets {resets} {zone}")


class AdminCli:
    def __init__(self, config_getter: Callable[[], Any], ledger: Any,
                 clock: Callable[[], datetime] | None = None,
                 out: TextIO | None = None, err: TextIO | None = None) -> None:
        self._config = config_getter
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._out = out
        self._err = err

    # argparse wiring -------------------------------------------------------
    def setup(self, parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(dest="quota_action", metavar="{status,reset,credit}")
        st = sub.add_parser("status", help="show windows, limits, spend and recent transactions")
        st.add_argument("user_id", nargs="?", help="Discord user id (all users when omitted)")
        rs = sub.add_parser("reset", help="zero the current window spend with an offsetting credit")
        rs.add_argument("user_id")
        rs.add_argument("--daily", action="store_true", help="reset the daily window")
        rs.add_argument("--weekly", action="store_true", help="reset the weekly window")
        cr = sub.add_parser("credit", help="insert a credit (negative cost) in the current window")
        cr.add_argument("user_id")
        cr.add_argument("amount_usd", help="positive USD amount")

    def run(self, args: argparse.Namespace) -> int:
        action = getattr(args, "quota_action", None)
        try:
            if action == "status":
                return self.status(getattr(args, "user_id", None))
            if action == "reset":
                return self.reset(args.user_id, daily=args.daily, weekly=args.weekly)
            if action == "credit":
                return self.credit(args.user_id, args.amount_usd)
        except Exception as exc:  # DB / config problems: report, do not traceback
            self._print(f"error: {exc}", err=True)
            return 1
        self._print("usage: hermes quota {status,reset,credit} ...", err=True)
        return 2

    # helpers ---------------------------------------------------------------
    def _print(self, text: str, err: bool = False) -> None:
        stream = (self._err or sys.stderr) if err else (self._out or sys.stdout)
        print(text, file=stream)

    @staticmethod
    def _clean_user(user_id: Any) -> str:
        return str(user_id).strip()

    # commands --------------------------------------------------------------
    def status(self, user_id: str | None = None) -> int:
        config = self._config()
        now = self._clock()
        if user_id is not None:
            uid = self._clean_user(user_id)
            if not uid:
                self._print("error: user id must not be empty", err=True)
                return 2
            self._print(self.render_user(uid, config, now))
            return 0
        users = self._ledger.user_ids()
        if not users:
            self._print("No quota transactions recorded.")
            return 0
        lines = [f"Discord quota status ({len(users)} users)"]
        for uid in users:
            s = evaluate(uid, config, self._ledger, now)
            lines.append(f"{uid}  daily {_money(s.daily.spent)} / {_limit(s.daily)}  "
                         f"weekly {_money(s.weekly.spent)} / {_limit(s.weekly)}  {_state(s)}")
        self._print("\n".join(lines))
        return 0

    def render_user(self, uid: str, config: Any, now: datetime) -> str:
        s = evaluate(uid, config, self._ledger, now)
        lines = [f"User {uid}  ({_state(s)})", "Windows:",
                 _window_line(s.daily, config.timezone), _window_line(s.weekly, config.timezone),
                 f"Recent transactions (latest {RECENT_N}):"]
        recent = self._ledger.recent(uid, RECENT_N)
        if not recent:
            lines.append("  none")
        for t in recent:
            lines.append(f"  #{t.id} {_fmt_ts(t.timestamp, config.timezone)}  {_money(t.cost_usd):>10}  "
                         f"{t.source_kind}  {t.provider}/{t.model}")
        return "\n".join(lines)

    def reset(self, user_id: str, *, daily: bool = False, weekly: bool = False) -> int:
        uid = self._clean_user(user_id)
        if not uid:
            self._print("error: user_id must not be empty", err=True)
            return 2
        if not daily and not weekly:
            daily = weekly = True
        config = self._config()
        now = self._clock()
        d_start, d_end = daily_window(now, config.timezone)
        w_start, w_end = weekly_window(now, config.timezone)
        if daily:
            credited = self._ledger.reset_window(uid, d_start, d_end, timestamp=now)
            self._print(f"Daily window reset for {uid}: credited {_money(credited)}.")
        if weekly:
            before_daily = self._ledger.spend_since(uid, d_start, d_end)
            # Credit stamped at the weekly window start: leaves the daily window
            # alone unless both windows start at the same instant.
            credited = self._ledger.reset_window(uid, w_start, w_end, timestamp=w_start)
            self._print(f"Weekly window reset for {uid}: credited {_money(credited)}.")
            after_daily = self._ledger.spend_since(uid, d_start, d_end)
            if after_daily != before_daily:
                self._print("The daily window starts with the weekly window, so it was reduced "
                            f"too ({_money(before_daily)} -> {_money(after_daily)}).")
        return 0

    def credit(self, user_id: str, amount: Any) -> int:
        uid = self._clean_user(user_id)
        if not uid:
            self._print("error: user_id must not be empty", err=True)
            return 2
        try:
            value = parse_amount(amount)
        except ValueError as exc:
            self._print(f"error: {exc}", err=True)
            return 2
        now = self._clock()
        self._ledger.add_credit(uid, value, timestamp=now, source_kind="admin_credit")
        self._print(f"Credited {_money(value)} to {uid}.")
        return 0


def _state(s: QuotaStatus) -> str:
    if not s.enabled:
        return "disabled"
    if s.blocking:
        return "blocked" if any(w.state is WindowState.BLOCKED for w in s.blocking) else "exhausted"
    return "warning" if s.warn else "ok"
