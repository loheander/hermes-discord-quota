"""Resolve the Discord user to charge for a hook call (PLAN.md section 6.2).

Lookup order, no I/O:

1. Task-local session vars via ``gateway.session_context.get_session_env``
   (``HERMES_SESSION_PLATFORM`` / ``HERMES_SESSION_USER_ID``). These are
   ContextVars bound per inbound message by the gateway, inherited by
   ``run_in_executor`` threads, with an ``os.environ`` fallback. They can be
   absent in other threads or in a delegated child, hence the maps below.
   A bound non-Discord platform is authoritative: it resolves to ``None``.
2. Bounded LRU maps ``turn_id`` / ``session_id`` / ``session_key`` populated by
   hooks via ``bind_turn`` / ``bind_session``.
3. ``child_session_id -> parent`` (``bind_child``) for subagents, whose own
   platform is ``"subagent"``.

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


def _hermes_env(name: str, default: str = "") -> str:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return default
    try:
        return get_session_env(name, default)
    except Exception:
        return default


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
                 env_getter: Callable[[str, str], str] | None = None) -> None:
        self._env = env_getter or _hermes_env
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
            if ident is not None:
                self._children.put(child, ident)
            elif _norm(parent_session_id):
                self._children.put(child, _norm(parent_session_id))

    # -- query ------------------------------------------------------------
    def resolve(self, session_id: str | None = None, turn_id: str | None = None,
                session_key: str | None = None) -> str | None:
        """Discord user id to charge, or ``None`` (non-Discord / unknown)."""
        platform = _norm(self._env("HERMES_SESSION_PLATFORM", "")).lower()
        user = _norm(self._env("HERMES_SESSION_USER_ID", ""))
        if platform:
            return user if platform == DISCORD and user else None
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
