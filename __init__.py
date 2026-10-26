"""Hermes directory-plugin entry point.

Hermes imports this directory as a package and calls ``register(ctx)``.
All logic lives in the ``discord_quota`` subpackage; keep this file thin.
"""

if __package__:
    from .discord_quota import register
else:  # pytest imports the repo root as a bare module during collection
    from discord_quota import register

__all__ = ["register"]
