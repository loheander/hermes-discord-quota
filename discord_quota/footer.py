"""``transform_llm_output`` footer (PLAN.md section 6.6).

Verified in ``agent/turn_finalizer.py``: the hook is synchronous, receives
``response_text, session_id, model, platform, turn_id``; the first non-empty
string returned replaces the response, ``None`` leaves it unchanged.
Discord status is decided only through the resolver, never ``platform``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .messages import append_footer, warning_footer
from .status import evaluate

logger = logging.getLogger("hermes_discord_quota")


class FooterHook:
    def __init__(self, config_getter: Callable[[], Any], ledger: Any, resolver: Any,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._config = config_getter
        self._ledger = ledger
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def transform_llm_output(self, response_text: str = "", session_id: str = "",
                             turn_id: str = "", **_: Any) -> str | None:
        try:
            if not isinstance(response_text, str) or not response_text.strip():
                return None
            user = self._resolver.resolve(session_id=session_id or None, turn_id=turn_id or None)
            if not user:
                return None
            config = self._config()
            if not getattr(config, "enabled", False):
                return None
            now = self._clock()
            status = evaluate(user, config, self._ledger, now)
            return append_footer(response_text, warning_footer(status, now, config.timezone))
        except Exception:
            logger.warning("discord_quota: footer failed; response unchanged", exc_info=True)
            return None
