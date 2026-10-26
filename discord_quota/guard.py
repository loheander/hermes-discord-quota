"""Mid-turn exhaustion guard (PLAN.md sections 6.3 step 5 and 6.5).

``after_record`` runs from the ``post_api_request`` recorder: it re-evaluates
the user's windows and, when a limited window is exhausted and the response has
pending tool calls, requests one hard interrupt on the running agent.
``pre_tool_call`` blocks every tool for a user who is not admitted.

Verified against Hermes Agent source: ``pre_tool_call`` receives ``tool_name,
args, task_id, session_id, tool_call_id, turn_id, ...`` and a returned
``{"action": "block", "message": ...}`` becomes the tool result (hook
exceptions are swallowed by Hermes, i.e. fail open there, so this hook catches
its own errors and fails closed). The running agent is found in
``gateway._running_agents`` (session key -> agent, possibly the pending
sentinel) and stopped with ``agent.interrupt_compat.request_hard_interrupt``.

Blocking uses ``status.admitted`` (not ``status.exhausted``) so BLOCKED
(limit 0) windows also stop tools. Unlimited users are always admitted.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from .messages import unavailable_reply
from .status import evaluate

logger = logging.getLogger("hermes_discord_quota")

BLOCK_MESSAGE = (
    "Quota exceeded mid-turn: Daily/Weekly USD spending limit reached. "
    "Stop calling tools and inform the user that their turn was halted due to "
    "quota exhaustion."
)
INTERRUPT_REASON = "quota exhausted"
_MAX_TURNS = 256


def _default_interrupt(agent: Any, message: str | None = None, *, tool_reason: str | None = None) -> bool:
    from agent.interrupt_compat import request_hard_interrupt
    return request_hard_interrupt(agent, message, tool_reason=tool_reason)


class MidTurnGuard:
    def __init__(self, config_getter: Callable[[], Any], ledger: Any, resolver: Any,
                 gateway_getter: Callable[[], Any] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 interrupter: Callable[..., bool] | None = None,
                 session_key_getter: Callable[[], str] | None = None) -> None:
        self._config = config_getter
        self._ledger = ledger
        self._resolver = resolver
        self._gateway = gateway_getter or (lambda: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interrupt = interrupter or _default_interrupt
        self._session_key = session_key_getter or _context_session_key
        self._lock = threading.Lock()
        self._exhausted_users: set[str] = set()
        self._interrupted: "OrderedDict[tuple, bool]" = OrderedDict()

    # -- state ------------------------------------------------------------
    def is_exhausted(self, user_id: str) -> bool:
        with self._lock:
            return str(user_id) in self._exhausted_users

    def _status(self, user_id: str) -> Any:
        status = evaluate(user_id, self._config(), self._ledger, self._clock())
        with self._lock:
            if status.admitted:  # reset or credit: clear the marker
                self._exhausted_users.discard(str(user_id))
            else:
                self._exhausted_users.add(str(user_id))
        return status

    # -- post_api_request step 5 ------------------------------------------
    def after_record(self, user_id: str, session_id: str, turn_id: str | None,
                     tool_call_count: int = 0) -> None:
        try:
            status = self._status(user_id)
            if status.admitted or tool_call_count <= 0:
                return
            self._request_interrupt(session_id, turn_id)
        except Exception:
            logger.warning("discord_quota: mid-turn exhaustion check failed", exc_info=True)

    # -- pre_tool_call ------------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", session_id: str = "", turn_id: str = "",
                      **_: Any) -> dict | None:
        try:
            user = self._resolver.resolve(session_id=session_id or None, turn_id=turn_id or None)
            if not user:
                return None  # not Discord (or unknown): never blocked
            if not getattr(self._config(), "enabled", False):
                return None
            if self._status(user).admitted:
                return None
        except Exception:
            logger.warning("discord_quota pre_tool_call check failed; blocking (fail closed)",
                           exc_info=True)
            return {"action": "block", "message": unavailable_reply()}
        self._request_interrupt(session_id, turn_id)
        return {"action": "block", "message": BLOCK_MESSAGE}

    # -- hard interrupt -----------------------------------------------------
    def _request_interrupt(self, session_id: str, turn_id: str | None) -> None:
        """At most once per turn; failures are logged, never raised."""
        key = (session_id or "", turn_id or "")
        with self._lock:
            if key in self._interrupted:
                return
            self._interrupted[key] = True
            while len(self._interrupted) > _MAX_TURNS:
                self._interrupted.popitem(last=False)
        try:
            agent = self._find_agent(session_id)
            if agent is None:
                logger.warning("discord_quota: no running agent to interrupt for session %s",
                               session_id)
                return
            if not self._interrupt(agent, None, tool_reason=INTERRUPT_REASON):
                logger.warning("discord_quota: agent does not support interrupt")
        except Exception:
            logger.warning("discord_quota: hard interrupt failed", exc_info=True)

    def _find_agent(self, session_id: str) -> Any:
        gateway = self._gateway()
        running = getattr(gateway, "_running_agents", None) if gateway is not None else None
        if not running:
            return None
        key = self._session_key()
        if key:
            agent = running.get(key)
            if agent is not None and hasattr(agent, "interrupt"):
                return agent
        if session_id:
            for agent in list(running.values()):
                if getattr(agent, "session_id", None) == session_id:
                    return agent
        return None


def _context_session_key() -> str:
    from .admission import AdmissionGate
    return AdmissionGate._context_session_key()
