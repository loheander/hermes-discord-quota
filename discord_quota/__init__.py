"""Implementation package for hermes-discord-quota.

Hermes imports are lazy (inside functions) so this package imports without
Hermes Agent on ``sys.path``.
"""

from .plugin import register

__all__ = ["register"]
