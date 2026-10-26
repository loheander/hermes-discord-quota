"""``subagent_stop``: charge delegated child spend to the parent's user (PLAN.md 6.4, 6.2 tier 3).

Payload verified against ``tools/delegate_tool_results.py``: parent_session_id,
parent_turn_id, child_session_id (plus child_role/summary/status, no cost). It
fires on the parent's thread, once per child. The payload carries no cost, so
the child's total is read from Hermes' ``sessions`` table (``estimated_cost_usd``,
falling back to ``actual_cost_usd``) via an injectable ``cost_lookup``.

Dedupe: if the ledger already holds any row for the child session (recorded per
call by ``post_api_request``) or a prior 'subagent' row, nothing is written.
Discord status and user come only from ``UserResolver.resolve``.

Known gap: if the child's cost is not yet persisted (0/None) nothing is
recorded and the hook is not marked done, so a later firing can still record it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .ledger import round_cost

logger = logging.getLogger("hermes_discord_quota")


def _default_cost_lookup(child_session_id: str) -> tuple[Decimal | None, str, str]:
    """(cost, model, provider) of a child session from Hermes' state.db."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "state.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT estimated_cost_usd, actual_cost_usd, model, billing_provider"
            " FROM sessions WHERE id = ?", (child_session_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None, "", ""
    cost = None
    for raw in (row[1], row[0]):
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if value.is_finite() and value > 0:
            cost = value
            break
    return cost, str(row[2] or ""), str(row[3] or "")


class SubagentAccountant:
    def __init__(self, ledger: Any, resolver: Any,
                 cost_lookup: Callable[[str], tuple] | None = None) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._lookup = cost_lookup or _default_cost_lookup
        self._lock = threading.Lock()

    def subagent_stop(self, **kw: Any) -> None:
        try:
            self._handle(kw)
        except Exception:
            logger.warning("discord_quota: failed to account subagent spend", exc_info=True)

    def _handle(self, kw: dict) -> None:
        child = str(kw.get("child_session_id") or "")
        if not child:
            return
        user = self._resolver.resolve(session_id=kw.get("parent_session_id") or child,
                                      turn_id=kw.get("parent_turn_id") or None)
        if not user:
            return
        with self._lock:
            if self._ledger.has_session(child):
                return
            cost, model, provider = self._lookup(child)
            if cost is None or Decimal(str(cost)) <= 0:
                return
            self._ledger.record(user, child, round_cost(cost), model, provider,
                                source_kind="subagent")
