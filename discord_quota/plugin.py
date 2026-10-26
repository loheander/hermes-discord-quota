"""Plugin wiring: ``register(ctx)`` attaches hooks and commands to Hermes.

Later stories add hook modules and register them here (PLAN.md section 2).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hermes_discord_quota")


CONFIG_TTL_SECONDS = 30.0


def _ttl_config_getter(loader: Any, ttl: float = CONFIG_TTL_SECONDS, clock: Any = None):
    """Reload config at most every ``ttl`` seconds so edits apply without a restart."""
    import time
    clock = clock or time.monotonic
    cache: list = []

    def get() -> Any:
        now = clock()
        if not cache or now - cache[0] >= ttl:
            cache[:] = [now, loader()]
        return cache[1]

    return get


def register(ctx: Any) -> None:
    """Called once by Hermes with a ``PluginContext``."""
    from .admission import AdmissionGate
    from .cli import CLI_DESCRIPTION, AdminCli
    from .command import DESCRIPTION, QuotaCommand
    from .config import load_config
    from .footer import FooterHook
    from .guard import MidTurnGuard
    from .ledger import Ledger, default_db_path
    from .recorder import SpendRecorder
    from .resolver import UserResolver
    from .subagent import SubagentAccountant

    getter = _ttl_config_getter(load_config)
    try:
        ledger = Ledger(default_db_path())
    except Exception:  # Hermes home unavailable (tests); resolved lazily on first use
        logger.warning("discord_quota: could not resolve ledger path", exc_info=True)
        ledger = Ledger()
    resolver = UserResolver()
    gate = AdmissionGate(getter, ledger, resolver)
    guard = MidTurnGuard(getter, ledger, resolver, gateway_getter=lambda: gate.gateway)
    recorder = SpendRecorder(ledger, resolver, after_record=guard.after_record)
    ctx.register_hook("pre_gateway_dispatch", gate.pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", gate.pre_llm_call)
    ctx.register_hook("subagent_start", gate.subagent_start)
    ctx.register_hook("on_session_end", gate.on_session_end)
    ctx.register_hook("post_api_request", recorder.post_api_request)
    ctx.register_hook("subagent_stop", SubagentAccountant(ledger, resolver, after_record=guard.after_record).subagent_stop)
    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    ctx.register_hook("transform_llm_output", FooterHook(getter, ledger, resolver).transform_llm_output)
    command = QuotaCommand(getter, ledger, resolver)
    ctx.register_hook("pre_gateway_dispatch", command.observe_dispatch)
    ctx.register_command("quota", command.handle, description=DESCRIPTION)
    admin = AdminCli(getter, ledger)
    ctx.register_cli_command("quota", "Inspect and adjust Discord user spend quotas",
                             admin.setup, admin.run, description=CLI_DESCRIPTION)
