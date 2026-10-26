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
from datetime import datetime, timezone
from typing import Any, Callable

from .messages import blocked_reply, is_blocked, quota_exceeded_reply
from .resolver import DISCORD, UserResolver
from .status import evaluate

logger = logging.getLogger("hermes_discord_quota")

BYPASS_COMMANDS = frozenset({"quota", "help", "whoami", "stop"})
SKIP_REASON = "discord_quota_exceeded"
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

    # -- pre_gateway_dispatch -------------------------------------------
    def pre_gateway_dispatch(self, event: Any = None, gateway: Any = None,
                             session_store: Any = None, **_: Any) -> dict | None:
        try:
            return self._gate(event, gateway, session_store)
        except Exception:
            logger.warning("discord_quota admission gate failed", exc_info=True)
            return None

    def _gate(self, event: Any, gateway: Any, session_store: Any) -> dict | None:
        config = self._config()
        if not getattr(config, "enabled", False):
            return None
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

        key = _session_key(session_store, source)
        if key:
            self._resolver.bind_session(DISCORD, user_id, session_key=key)

        now = self._clock()
        try:
            status = evaluate(user_id, config, self._ledger, now)
        except Exception:
            logger.warning("discord_quota status evaluation failed; admitting", exc_info=True)
            return None
        if status.admitted:
            return None

        message = (blocked_reply() if is_blocked(status)
                   else quota_exceeded_reply(status, now, config.timezone))
        self._deliver(gateway, source, message)
        return dict(SKIP)

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
            await adapter.send(source.chat_id, message, metadata=metadata)
        except Exception:
            logger.warning("Failed to deliver quota rejection on Discord", exc_info=True)

    # -- resolver binding hooks ------------------------------------------
    def pre_llm_call(self, session_id: str = "", turn_id: str = "", platform: str = "",
                     sender_id: str = "", **_: Any) -> None:
        try:
            self._resolver.bind_turn(turn_id, platform, sender_id)
            if session_id and sender_id:
                self._resolver.bind_session(platform, sender_id, session_id=session_id)
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
