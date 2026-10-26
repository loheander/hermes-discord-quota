"""Resolve the Discord user to charge for a hook call (PLAN.md section 6.2).

Lookup order, no I/O:

1. Task-local ContextVars ``HERMES_SESSION_PLATFORM`` / ``HERMES_SESSION_USER_ID``
   in ``gateway.session_context``, read directly (never ``get_session_env``,
   which falls back to process-global ``os.environ``; ``os.environ`` is never
   read). An unset or empty var counts as absent. A non-empty non-Discord
   platform is authoritative (``None``). Delegated children inherit the
   parent's ContextVars via ``copy_context``, so they normally resolve here.
2. Bounded LRU maps. The turn binding (``bind_turn``) is authoritative; the
   session_id / session_key entries (``bind_session``) are a last-resort hint
   only, since one session serves several users in shared guild channels and
   the map keeps the last writer. They are consulted only when no turn_id was
   given or the turn is unbound.
3. ``child_session_id -> parent`` (``bind_child``).

Downstream hooks must decide Discord status only via ``resolve()``, never via
the hook payload ``platform`` (for subagents that is the agent attribute
``"subagent"``, not the user's platform).

Intended wiring (hooks are not wired in this module):
- ``pre_gateway_dispatch`` has only event.source and the session store (no
  turn_id/session_id): at most ``bind_session(..., session_key=...)``.
- ``pre_llm_call`` (session_id, turn_id, platform, sender_id): ``bind_turn``.
- ``subagent_start`` (child_session_id, parent_session_id, parent_turn_id):
  ``bind_child``.

Hermes is imported lazily so the plugin imports without it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

DISCORD = "discord"
DEFAULT_MAX_ENTRIES = 4096
_MAX_PARENT_HOPS = 8


@dataclass(frozen=True)
class Identity:
    platform: str
    user_id: str


def _norm(value: object) -> str:
    return str(value or "").strip()


def _hermes_context() -> tuple[str, str]:
    """(platform, user_id) from the ContextVars only; ("", "") if unavailable."""
    try:
        from gateway import session_context as sc
        unset = sc._UNSET
        out = []
        for var in (sc._SESSION_PLATFORM, sc._SESSION_USER_ID):
            value = var.get()
            out.append("" if value is unset else _norm(value))
        return out[0], out[1]
    except Exception:
        return "", ""


class _LRU:
    """Bounded mapping; callers hold the resolver lock."""

    def __init__(self, max_entries: int) -> None:
        self.max = max(1, int(max_entries))
        self.data: OrderedDict[str, object] = OrderedDict()

    def put(self, key: str, value: object) -> None:
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.max:
            self.data.popitem(last=False)

    def get(self, key: str) -> object | None:
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key]

    def __len__(self) -> int:
        return len(self.data)


class UserResolver:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 context_getter: Callable[[], tuple[str, str]] | None = None) -> None:
        self._context = context_getter or _hermes_context
        self._lock = threading.Lock()
        self._turns = _LRU(max_entries)
        self._sessions = _LRU(max_entries)   # session_id and session_key share it (distinct id spaces)
        self._children = _LRU(max_entries)   # child_session_id -> Identity | parent session_id (str)

    # -- population -------------------------------------------------------
    def bind_turn(self, turn_id: str, platform: str, user_id: str) -> None:
        if _norm(turn_id) and _norm(user_id):
            with self._lock:
                self._turns.put(_norm(turn_id), Identity(_norm(platform).lower(), _norm(user_id)))

    def bind_session(self, platform: str, user_id: str, session_id: str | None = None,
                     session_key: str | None = None) -> None:
        if not _norm(user_id):
            return
        ident = Identity(_norm(platform).lower(), _norm(user_id))
        with self._lock:
            if _norm(session_id):
                self._sessions.put("id:" + _norm(session_id), ident)
            if _norm(session_key):
                self._sessions.put("key:" + _norm(session_key), ident)

    def bind_child(self, child_session_id: str, parent_session_id: str | None = None,
                   parent_turn_id: str | None = None) -> None:
        """Link a subagent session to its parent. The parent's identity is
        snapshotted when already known so parent eviction cannot orphan it."""
        child = _norm(child_session_id)
        if not child:
            return
        with self._lock:
            ident = self._lookup_parent(_norm(parent_session_id), _norm(parent_turn_id))
            if ident is None:
                cp, cu = self._context()
                if _norm(cu) and _norm(cp):
                    ident = Identity(_norm(cp).lower(), _norm(cu))
                else:
                    ident = self._lookup_session_map(_norm(parent_session_id))
            if ident is not None:
                self._children.put(child, ident)
            elif _norm(parent_session_id):
                self._children.put(child, _norm(parent_session_id))

    # -- query ------------------------------------------------------------
    def resolve(self, session_id: str | None = None, turn_id: str | None = None,
                session_key: str | None = None) -> str | None:
        """Discord user id to charge, or ``None`` (non-Discord / unknown)."""
        platform, user = self._context()
        platform, user = _norm(platform).lower(), _norm(user)
        if platform and platform != DISCORD:
            return None
        if platform and user:
            return user
        with self._lock:
            ident = self._lookup(_norm(session_id), _norm(turn_id), _norm(session_key))
        return ident.user_id if ident and ident.platform == DISCORD else None

    def size(self) -> int:
        with self._lock:
            return len(self._turns) + len(self._sessions) + len(self._children)

    # -- internals (lock held) -------------------------------------------
    def _lookup_parent(self, session_id: str, turn_id: str) -> Identity | None:
        if turn_id:
            hit = self._turns.get(turn_id)
            if isinstance(hit, Identity):
                return hit
        return None

    def _lookup_session_map(self, session_id: str) -> Identity | None:
        return self._lookup_session(session_id) if session_id else None

    def _lookup_session(self, session_id: str) -> Identity | None:
        cur = session_id
        for _ in range(_MAX_PARENT_HOPS):
            hit = self._sessions.get("id:" + cur)
            if isinstance(hit, Identity):
                return hit
            hit = self._children.get(cur)
            if isinstance(hit, Identity):
                return hit
            if not isinstance(hit, str) or hit == cur:
                return None
            cur = hit
        return None

    def _lookup(self, session_id: str, turn_id: str, session_key: str) -> Identity | None:
        if turn_id:
            hit = self._turns.get(turn_id)
            if isinstance(hit, Identity):
                return hit
        if session_id:
            ident = self._lookup_session(session_id)
            if ident:
                return ident
        if session_key:
            hit = self._sessions.get("key:" + session_key)
            if isinstance(hit, Identity):
                return hit
        return None
