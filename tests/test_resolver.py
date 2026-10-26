import sys
import threading
import types

from discord_quota.resolver import UserResolver


def env(platform="", user=""):
    d = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user}
    return lambda name, default="": d.get(name, default)


def mk(platform="", user="", **kw):
    return UserResolver(env_getter=env(platform, user), **kw)


def test_context_var_wins_over_maps():
    r = mk("discord", "ctx")
    r.bind_session("discord", "mapped", session_id="s1")
    assert r.resolve(session_id="s1") == "ctx"


def test_maps_used_when_context_absent():
    r = mk()
    r.bind_session("discord", "u1", session_id="s1", session_key="k1")
    r.bind_turn("t1", "discord", "u2")
    assert r.resolve(session_id="s1") == "u1"
    assert r.resolve(session_key="k1") == "u1"
    assert r.resolve(session_id="s1", turn_id="t1") == "u2"
    assert r.resolve(session_id="nope") is None
    assert r.resolve() is None


def test_non_discord_is_none():
    assert mk("telegram", "9").resolve() is None
    r = mk()
    r.bind_session("telegram", "9", session_id="s")
    assert r.resolve(session_id="s") is None


def test_context_platform_non_discord_blocks_map():
    r = mk("subagent", "")
    r.bind_session("discord", "u1", session_id="s")
    assert r.resolve(session_id="s") is None


def test_child_resolves_to_parent():
    r = mk()
    r.bind_session("discord", "u1", session_id="p")
    r.bind_child("c", parent_session_id="p")
    r.bind_child("gc", parent_session_id="c")
    assert r.resolve(session_id="c") == "u1"
    assert r.resolve(session_id="gc") == "u1"


def test_child_bound_before_parent_known_and_cycle_safe():
    r = mk()
    r.bind_child("c", parent_session_id="p")
    assert r.resolve(session_id="c") is None
    r.bind_session("discord", "u1", session_id="p")
    assert r.resolve(session_id="c") == "u1"
    r.bind_child("a", parent_session_id="b")
    r.bind_child("b", parent_session_id="a")
    assert r.resolve(session_id="a") is None


def test_child_survives_parent_eviction_and_parent_turn():
    r = mk(max_entries=2)
    r.bind_turn("t", "discord", "u1")
    r.bind_child("c", parent_turn_id="t")  # no parent session id: snapshot only
    r.bind_session("discord", "x", session_id="a")
    r.bind_session("discord", "x", session_id="b")
    r.bind_session("discord", "x", session_id="d")
    assert r.resolve(session_id="c") == "u1"


def test_eviction_is_bounded_lru():
    r = mk(max_entries=3)
    for i in range(10):
        r.bind_turn(f"t{i}", "discord", f"u{i}")
    assert r.resolve(turn_id="t0") is None
    assert r.resolve(turn_id="t9") == "u9"
    assert r.size() == 3
    r.resolve(turn_id="t7")          # touch t7
    r.bind_turn("new", "discord", "n")
    assert r.resolve(turn_id="t7") == "u7"
    assert r.resolve(turn_id="t8") is None


def test_thread_safety():
    r = mk(max_entries=50)

    def work(n):
        for i in range(500):
            r.bind_turn(f"{n}-{i}", "discord", "u")
            r.bind_session("discord", "u", session_id=f"{n}-{i}")
            r.bind_child(f"c{n}-{i}", parent_session_id=f"{n}-{i}")
            r.resolve(session_id=f"{n}-{i}", turn_id=f"{n}-{i}")

    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert r.size() <= 150


def test_hermes_lookup_is_lazy_and_optional(monkeypatch):
    r = UserResolver()
    monkeypatch.setitem(sys.modules, "gateway", None)  # import fails
    r.bind_session("discord", "u1", session_id="s")
    assert r.resolve(session_id="s") == "u1"
    mod = types.ModuleType("gateway.session_context")
    vals = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_USER_ID": "42"}
    mod.get_session_env = lambda n, d="": vals.get(n, d)
    pkg = types.ModuleType("gateway"); pkg.session_context = mod
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    assert r.resolve(session_id="s") == "42"
