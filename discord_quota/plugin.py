"""Plugin wiring: ``register(ctx)`` attaches hooks and commands to Hermes.

Later stories add hook modules and register them here (PLAN.md section 2).
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Called once by Hermes with a ``PluginContext``. Nothing registered yet."""
