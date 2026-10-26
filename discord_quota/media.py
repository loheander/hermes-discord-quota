"""``post_tool_call``: charge image/video generation spend (PLAN.md sections 6.3, 6.5).

Hermes provider plugins serialise the provider-reported cost into the tool
result: ``plugins/image_gen/openrouter`` sets ``extra["cost_usd"]``, the video
plugin ``extra["cost"]``; ``success_response`` merges ``extra`` into the top
level. Both keys are accepted at the top level or under ``extra``.

The hook is synchronous, never raises, and resolves the user only through
``UserResolver.resolve``.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .config import provider_is_free
from .ledger import round_cost

logger = logging.getLogger("hermes_discord_quota")

MEDIA_TOOLS = frozenset({"image_generate", "video_generate"})
_COST_KEYS = ("cost_usd", "cost")


def _parse(result: Any) -> dict | None:
    if isinstance(result, dict):
        return result
    if isinstance(result, (str, bytes)):
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _cost(payload: dict) -> Decimal | None:
    extra = payload.get("extra")
    sources = [payload] + ([extra] if isinstance(extra, dict) else [])
    for src in sources:
        for key in _COST_KEYS:
            value = src.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue
            if amount.is_finite() and amount >= 0:
                return amount
    return None


class MediaSpendRecorder:
    def __init__(self, ledger: Any, resolver: Any, after_record: Callable | None = None,
                 config_getter: Callable | None = None) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._after_record = after_record
        self._config_getter = config_getter

    def post_tool_call(self, tool_name: str = "", result: Any = None, session_id: str = "",
                       turn_id: str = "", **_: Any) -> None:
        try:
            if tool_name in MEDIA_TOOLS:
                self._record(tool_name, result, session_id or "", turn_id or None)
        except Exception:
            logger.warning("discord_quota: failed to record media spend", exc_info=True)

    def _record(self, tool: str, result: Any, session_id: str, turn_id: str | None) -> None:
        payload = _parse(result)
        if payload is None or payload.get("success") is not True or payload.get("error"):
            return
        user = self._resolver.resolve(session_id=session_id or None, turn_id=turn_id)
        if not user:
            return
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")
        if provider_is_free(self._config_getter, provider):
            cost = Decimal(0)
        else:
            cost = _cost(payload)
            if cost is None:
                logger.warning("Unpriced media usage: %s on %s - accruing $0.00",
                               tool, provider or "?")
                cost = Decimal(0)
        self._ledger.record(user, session_id, round_cost(cost), model, provider,
                            turn_id=turn_id, source_kind="media")
        if self._after_record is not None:
            # A tool is mid-flight, so the loop will continue: treat as pending tool work.
            self._after_record(user, session_id, turn_id, 1)
