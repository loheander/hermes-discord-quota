"""Pre-turn admission gate and resolver-binding hooks (PLAN.md 6.1 steps 1-4, 6; 7.1, 7.2).

Verified against Hermes Agent source:

- ``pre_gateway_dispatch(event, gateway, session_store)`` is invoked
  *synchronously* by ``GatewayRunner._hm_pre_gateway_dispatch_hook`` (on the
  event loop thread, inside the async ``_handle_message``). Coroutine results
  are not awaited, so the hook is a plain function. ``{"action": "skip",
  "reason": ...}`` drops the event; ``None`` continues normal dispatch.
- ``adapter.send(chat_id, content, reply_to=None, metadata=None)`` has no
  ``thread_id`` kwarg (PLAN.md 6.1 is wrong): the thread goes in
  ``metadata={"thread_id": ...}``.
- Because the hook is sync, the send is scheduled on the running loop (kept
  alive in ``_pending``); with no running loop it is run to completion.
- ``pre_llm_call`` (session_id, turn_id, platform, sender_id) -> ``bind_turn``;
  ``subagent_start`` (parent_session_id, parent_turn_id, child_session_id, ...)
  -> ``bind_child``.

No exception escapes any hook.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from .messages import blocked_reply, concurrent_reply, is_blocked, quota_exceeded_reply, unavailable_reply
from .resolver import DISCORD, UserResolver
from .status import evaluate

logger = logging.getLogger("hermes_discord_quota")

BYPASS_COMMANDS = frozenset({"quota", "help", "whoami", "stop"})
SKIP_REASON = "discord_quota_exceeded"
UNAVAILABLE_REASON = "discord_quota_unavailable"
WARN_INTERVAL_SECONDS = 60.0
CONCURRENT_REASON = "discord_quota_concurrent"
LOW_BALANCE_USD = Decimal("0.10")
# A marker not yet visible in gateway._running_agents is trusted this long
# (the agent is registered after pre_gateway_dispatch); without the gateway
# map a marker expires after MARKER_MAX_AGE_SECONDS.
#
# Known limits: a marker is set for every admitted message, including ones that
# never start an LLM turn (slash commands, messages Hermes later rejects). Such
# markers get no session_id/on_session_end and linger for MARKER_GRACE_SECONDS,
# so they can briefly reject a low-balance user's real turn in another channel.
# Markers keyed by the ``chan:`` fallback (no session key available) are never
# in ``_running_agents``, so they are trusted for MARKER_MAX_AGE_SECONDS and
# otherwise cleared only by on_session_end.
MARKER_GRACE_SECONDS = 30.0
MARKER_MAX_AGE_SECONDS = 900.0
SKIP = {"action": "skip", "reason": SKIP_REASON}


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _session_key(session_store: Any, source: Any) -> str:
    try:
        gen = getattr(session_store, "_generate_session_key", None)
        return str(gen(source) or "") if callable(gen) else ""
    except Exception:
        return ""


class AdmissionGate:
    def __init__(self, config_getter: Callable[[], Any], ledger: Any, resolver: UserResolver,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._config = config_getter
        self._ledger = ledger
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pending: set[asyncio.Future] = set()
        self._last_warn = -WARN_INTERVAL_SECONDS
        self._mono = time.monotonic
        # user_id -> {session_key: [admitted_at, session_id]}; guarded by _lock
        self._inflight: dict[str, dict[str, list]] = {}
        self._lock = threading.Lock()

    # -- pre_gateway_dispatch -------------------------------------------
    def pre_gateway_dispatch(self, event: Any = None, gateway: Any = None,
                             session_store: Any = None, **_: Any) -> dict | None:
        try:
            target = self._classify(event)
        except Exception:
            logger.warning("discord_quota event classification failed", exc_info=True)
            return None
        if target is None:
            return None  # not a gated Discord event: never blocked
        source, user_id = target
        try:
            return self._admit(source, user_id, gateway, session_store)
        except Exception:
            return self._unavailable(source, gateway)

    @staticmethod
    def _classify(event: Any) -> tuple[Any, str] | None:
        """(source, user_id) for a Discord, non-bypass, non-bot event; else None."""
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            return None
        if _platform_name(source) != DISCORD:
            return None
        user_id = str(getattr(source, "user_id", "") or "").strip()
        if not user_id or getattr(source, "is_bot", False):
            return None
        try:
            command = event.get_command()
        except Exception:
            command = None
        if command in BYPASS_COMMANDS:
            return None
        return source, user_id

    def _admit(self, source: Any, user_id: str, gateway: Any, session_store: Any) -> dict | None:
        config = self._config()
        if not getattr(config, "enabled", False):
            return None
        key = _session_key(session_store, source)
        if key:
            self._resolver.bind_session(DISCORD, user_id, session_key=key)
        now = self._clock()
        status = evaluate(user_id, config, self._ledger, now)
        if status.admitted:
            return self._guard(status, user_id, key or f"chan:{getattr(source, 'chat_id', '')}:"
                               f"{getattr(source, 'thread_id', '')}", source, gateway)
        message = (blocked_reply() if is_blocked(status)
                   else quota_exceeded_reply(status, now, config.timezone))
        self._deliver(gateway, source, message)
        return dict(SKIP)

    # -- concurrency guard (PLAN.md 6.1 step 5) --------------------------
    @staticmethod
    def _min_remaining(status: Any) -> Decimal | None:
        rem = [w.remaining for w in status.windows
               if w.remaining is not None and w.state.value != "unlimited"]
        return min(rem) if rem else None

    @staticmethod
    def _running_keys(gateway: Any) -> Any:
        try:
            running = gateway._running_agents
            return running if hasattr(running, "__contains__") else None
        except Exception:
            return None

    def _alive(self, key: str, marker: list, running: Any, now: float) -> bool:
        age = now - marker[0]
        if running is None or key.startswith("chan:"):
            return age < MARKER_MAX_AGE_SECONDS
        try:
            return age < MARKER_GRACE_SECONDS or key in running
        except Exception:
            return age < MARKER_MAX_AGE_SECONDS

    def _guard(self, status: Any, user_id: str, key: str, source: Any,
               gateway: Any) -> dict | None:
        """Mark the turn in flight; reject a parallel one when the balance is low."""
        remaining = self._min_remaining(status)
        if remaining is None:
            return None  # unlimited users are never guarded or tracked
        running = self._running_keys(gateway)
        now = self._mono()
        with self._lock:
            mine = self._inflight.setdefault(user_id, {})
            for k in [k for k, m in mine.items() if not self._alive(k, m, running, now)]:
                del mine[k]
            others = [k for k in mine if k != key]
            if others and remaining < LOW_BALANCE_USD:
                reject = True
            else:
                reject = False
                cur = mine.get(key)
                if cur is None:
                    mine[key] = [now, None]
                elif cur[1] is None:
                    cur[0] = now  # same-key re-admission keeps a running turn's marker
            if not mine:
                self._inflight.pop(user_id, None)
        if not reject:
            return None
        self._deliver(gateway, source, concurrent_reply(remaining))
        return {"action": "skip", "reason": CONCURRENT_REASON}

    def inflight_count(self, user_id: str) -> int:
        with self._lock:
            return len(self._inflight.get(str(user_id), {}))

    @staticmethod
    def _context_session_key() -> str:
        """Gateway session key of the current turn (ContextVar), or ""."""
        try:
            from gateway import session_context as sc
            value = sc._SESSION_KEY.get()
            return "" if value is sc._UNSET else str(value or "").strip()
        except Exception:
            return ""

    def _attach_session(self, user_id: str, session_id: str, session_key: str = "") -> None:
        """Bind session_id to the marker of its own channel.

        Without the session key, bind only when exactly one marker is unattached
        (otherwise the owner is ambiguous and the marker ages out instead).
        """
        with self._lock:
            mine = self._inflight.get(user_id, {})
            if any(m[1] == session_id for m in mine.values()):
                return
            if session_key:
                marker = mine.get(session_key)
                if marker is not None and marker[1] is None:
                    marker[1] = session_id
                return
            free = [m for m in mine.values() if m[1] is None]
            if len(free) == 1:
                free[0][1] = session_id

    def _release_session(self, session_id: str) -> None:
        with self._lock:
            for user_id in list(self._inflight):
                mine = self._inflight[user_id]
                for k in [k for k, m in mine.items() if m[1] == session_id]:
                    del mine[k]
                if not mine:
                    del self._inflight[user_id]

    def on_session_end(self, session_id: str = "", **_: Any) -> None:
        """Turn end (fires every turn, also interrupted/failed): clear the marker."""
        try:
            if session_id:
                self._release_session(str(session_id))
        except Exception:
            logger.warning("discord_quota in-flight release failed", exc_info=True)
        return None

    def _unavailable(self, source: Any, gateway: Any) -> dict:
        """Fail closed: the quota check itself failed for a gated event."""
        mono = time.monotonic()
        if mono - self._last_warn >= WARN_INTERVAL_SECONDS:
            self._last_warn = mono
            logger.warning("discord_quota check failed; rejecting turn (fail closed)",
                           exc_info=True)
        try:
            self._deliver(gateway, source, unavailable_reply())
        except Exception:
            pass
        return {"action": "skip", "reason": UNAVAILABLE_REASON}

    # -- delivery ---------------------------------------------------------
    def _deliver(self, gateway: Any, source: Any, message: str) -> None:
        try:
            adapter = gateway._adapter_for_source(source)
            if adapter is None:
                logger.warning("discord_quota: no adapter to deliver quota rejection")
                return
            coro = self._send(adapter, source, message)
        except Exception:
            logger.warning("Failed to deliver quota rejection on Discord", exc_info=True)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(coro)
            except Exception:
                logger.warning("Failed to deliver quota rejection on Discord", exc_info=True)
            return
        task = loop.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @staticmethod
    async def _send(adapter: Any, source: Any, message: str) -> None:
        thread_id = getattr(source, "thread_id", None)
        metadata = {"thread_id": thread_id} if thread_id is not None else None
        try:
            result = await adapter.send(source.chat_id, message, metadata=metadata)
            if getattr(result, "success", True) is False:
                logger.warning("Quota rejection not delivered on Discord: %s",
                               getattr(result, "error", None))
        except Exception:
            logger.warning("Failed to deliver quota rejection on Discord", exc_info=True)

    # -- resolver binding hooks ------------------------------------------
    def pre_llm_call(self, session_id: str = "", turn_id: str = "", platform: str = "",
                     sender_id: str = "", **_: Any) -> None:
        try:
            self._resolver.bind_turn(turn_id, platform, sender_id)
            if session_id and sender_id:
                self._resolver.bind_session(platform, sender_id, session_id=session_id)
                self._attach_session(str(sender_id).strip(), str(session_id),
                                     self._context_session_key())
        except Exception:
            logger.warning("discord_quota bind_turn failed", exc_info=True)
        return None

    def subagent_start(self, child_session_id: str = "", parent_session_id: str = "",
                       parent_turn_id: str = "", **_: Any) -> None:
        try:
            self._resolver.bind_child(child_session_id, parent_session_id, parent_turn_id)
        except Exception:
            logger.warning("discord_quota bind_child failed", exc_info=True)
        return None
