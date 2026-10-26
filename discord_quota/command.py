"""``/quota`` slash command (PLAN.md section 7.3).

Verified in Hermes: ``ctx.register_command(name, handler, description=...)``
handlers are called as ``handler(raw_args)`` (sync or async) from
``_hm_dispatch_quick_and_plugin_commands``, with no user argument, and before
the per-turn session ContextVars are bound. The invoker is therefore captured
by a ``pre_gateway_dispatch`` observer (``observe_dispatch``, never blocks) that
stores the Discord sender in a plugin-owned ContextVar; the observer and the
handler run in the same ``_handle_message`` task. The resolver (turn
ContextVars) is consulted first.
"""

from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .messages import quota_status_text, unavailable_reply
from .status import evaluate

logger = logging.getLogger("hermes_discord_quota")

DESCRIPTION = "Visa din budget på Discord"
NOT_DISCORD_REPLY = "/quota visar din budget och fungerar bara när du skriver till boten på Discord."

_invoker: contextvars.ContextVar[str] = contextvars.ContextVar("discord_quota_invoker", default="")


class QuotaCommand:
    def __init__(self, config_getter: Callable[[], Any], ledger: Any, resolver: Any,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._config = config_getter
        self._ledger = ledger
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def observe_dispatch(self, event: Any = None, **_: Any) -> None:
        """pre_gateway_dispatch observer: remember the Discord sender; always returns None."""
        try:
            user = ""
            source = getattr(event, "source", None)
            platform = getattr(source, "platform", None)
            name = str(getattr(platform, "value", platform) or "").strip().lower()
            if name == "discord" and not getattr(source, "is_bot", False):
                user = str(getattr(source, "user_id", "") or "").strip()
            _invoker.set(user)
        except Exception:
            logger.warning("discord_quota: could not record /quota invoker", exc_info=True)
        return None

    def handle(self, raw_args: str = "") -> str:
        try:
            user = self._resolver.resolve() or _invoker.get()
            if not user:
                return NOT_DISCORD_REPLY
            config = self._config()
            now = self._clock()
            status = evaluate(user, config, self._ledger, now)
            return quota_status_text(status, now, config.timezone)
        except Exception:
            logger.warning("discord_quota: /quota failed", exc_info=True)
            return unavailable_reply()
