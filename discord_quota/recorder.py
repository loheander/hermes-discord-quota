"""``post_api_request``: price each model call and record it (PLAN.md section 6.3 steps 1-4).

The hook is synchronous and runs on the agent thread; Hermes swallows nothing
we raise reliably, so every failure is logged here and never propagates.
Discord status and user come only from ``UserResolver.resolve``; the payload
``platform`` is never consulted (child agents report "subagent").

Payload keys verified against ``agent/turn_response_intake.py``: session_id,
turn_id, task_id, platform, model, provider, base_url, usage (CanonicalUsage
summary dict), moa_references (list of per-advisor dicts with model, provider,
usage, cost_usd, cost_status, or None).

Known gaps: Hermes MoA leaves ``_last_reference_metrics`` set on fan-out cache
hits, so identical ``moa_references`` repeat on later calls of one turn; advisor
charges are deduplicated per (session_id, turn_id) by fingerprint. The MoA
aggregator's virtual model/provider have no pricing entry, so the aggregator
call itself accrues $0 (with an "Unpriced model" warning), and advisor tokens
are not included in ledger token counts. Pricing via the real aggregator slot
is not done here.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .ledger import round_cost

from .config import provider_is_free

logger = logging.getLogger("hermes_discord_quota")

_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                 "cache_write_tokens", "reasoning_tokens")


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _default_estimator(model: str, usage: dict, provider: str | None, base_url: str | None):
    """Price with Hermes; returns (Decimal | None, status)."""
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    canonical = CanonicalUsage(**{f: _int(usage.get(f)) for f in _USAGE_FIELDS})
    result = estimate_usage_cost(model, canonical, provider=provider, base_url=base_url)
    return result.amount_usd, getattr(result, "status", None)


class SpendRecorder:
    def __init__(self, ledger: Any, resolver: Any, estimator: Callable | None = None,
                 after_record: Callable | None = None,
                 config_getter: Callable | None = None) -> None:
        self._config_getter = config_getter
        self._after_record = after_record
        self._ledger = ledger
        self._resolver = resolver
        self._estimate = estimator or _default_estimator
        self._seen_refs: "OrderedDict[tuple, set[str]]" = OrderedDict()
        self._max_turns = 256
        self._lock = threading.Lock()

    def _fresh_refs(self, key: tuple, refs: Any) -> list:
        """Refs not yet charged for this turn (MoA repeats refs on cache hits).

        Dedupes only against fingerprints from previous calls, so identical
        refs inside one list are all charged. Skipped when turn_id is missing.
        """
        if not isinstance(refs, (list, tuple)):
            return []
        if not key[1]:
            return list(refs)
        with self._lock:
            seen = self._seen_refs.get(key)
            if seen is None:
                seen = self._seen_refs[key] = set()
                while len(self._seen_refs) > self._max_turns:
                    self._seen_refs.popitem(last=False)
            else:
                self._seen_refs.move_to_end(key)
            previous = set(seen)
            fresh = []
            for ref in refs:
                try:
                    fp = json.dumps(ref, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    fp = repr(ref)
                if fp in previous:
                    continue
                seen.add(fp)
                fresh.append(ref)
            return fresh

    def _price(self, model: str, usage: dict, provider: str | None,
               base_url: str | None) -> Decimal | None:
        """Cost in USD, or None when unpriced/unknown."""
        try:
            amount, status = self._estimate(model, usage, provider, base_url)
        except Exception:
            logger.warning("discord_quota: pricing failed for %s", model, exc_info=True)
            return None
        if amount is None or status == "unknown":
            return None
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() else None

    def _is_free(self, provider: Any) -> bool:
        return provider_is_free(self._config_getter, provider)

    def _moa_cost(self, refs: Any) -> Decimal:
        total = Decimal(0)
        if not isinstance(refs, (list, tuple)):
            return total
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if self._is_free(ref.get("provider")):
                continue
            usage = ref.get("usage") or {}
            model = str(ref.get("model") or "")
            cost = self._price(model, usage, ref.get("provider"), None) if model else None
            if cost is None and ref.get("cost_usd") is not None:
                try:
                    alt = Decimal(str(ref["cost_usd"]))
                    cost = alt if alt.is_finite() else None
                except (InvalidOperation, ValueError):
                    cost = None
            if cost is None:
                logger.warning("Unpriced model usage: %s on %s - accruing $0.00",
                               model or "?", ref.get("provider") or "?")
                continue
            total += cost
        return total

    def post_api_request(self, **kw: Any) -> None:
        try:
            self._record(kw)
        except Exception:
            logger.warning("discord_quota: failed to record API spend", exc_info=True)

    def _record(self, kw: dict) -> None:
        session_id = kw.get("session_id") or ""
        turn_id = kw.get("turn_id") or None
        user = self._resolver.resolve(session_id=session_id, turn_id=turn_id)
        if not user:
            return
        model = str(kw.get("model") or "")
        provider = str(kw.get("provider") or "")
        usage = kw.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        if self._is_free(provider):  # PLAN.md 3: free provider answered this call
            cost = Decimal(0)
        else:
            cost = self._price(model, usage, provider or None, kw.get("base_url"))
        if cost is None:
            logger.warning("Unpriced model usage: %s on %s - accruing $0.00",
                           model or "?", provider or "?")
            cost = Decimal(0)
        cost += self._moa_cost(self._fresh_refs((session_id, turn_id), kw.get("moa_references")))
        total = round_cost(cost)
        input_tokens = _int(usage.get("input_tokens")) + _int(usage.get("cache_read_tokens")) \
            + _int(usage.get("cache_write_tokens"))
        self._ledger.record(user, session_id, total, model, provider, turn_id=turn_id,
                            input_tokens=input_tokens,
                            output_tokens=_int(usage.get("output_tokens")))
        if self._after_record is not None:  # PLAN.md 6.3 step 5; never raises
            self._after_record(user, session_id, turn_id, _int(kw.get("assistant_tool_call_count")))
