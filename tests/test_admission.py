import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from discord_quota.admission import AdmissionGate
from discord_quota.config import Limits, QuotaConfig
from discord_quota.resolver import UserResolver

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class FakeLedger:
    def __init__(self, spent="0", boom=False):
        self.spent, self.boom = Decimal(spent), boom

    def spend_since(self, user, start, end=None):
        if self.boom:
            raise RuntimeError("db")
        return self.spent


class FakeAdapter:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        if self.fail:
            raise RuntimeError("discord down")


class FakeGateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter


class FakeStore:
    def _generate_session_key(self, source):
        return "agent:main:discord:group:123"


class Event:
    def __init__(self, text="hello", source=None, internal=False):
        self.text, self.internal = text, internal
        self.source = source or src()

    def get_command(self):
        t = self.text.lstrip()
        return t.split()[0][1:].lower() if t.startswith("/") else None


def src(**kw):
    d = dict(platform=SimpleNamespace(value="discord"), user_id="42", chat_id="123",
             thread_id=None, is_bot=False)
    d.update(kw)
    return SimpleNamespace(**d)


def make(daily=1.0, weekly=5.0, spent="2", enabled=True, adapter=None, ledger=None):
    cfg = QuotaConfig(enabled=enabled, default=Limits(daily, weekly))
    adapter = adapter or FakeAdapter()
    resolver = UserResolver(context_getter=lambda: ("", ""))
    gate = AdmissionGate(lambda: cfg, ledger or FakeLedger(spent), resolver, clock=lambda: NOW)
    return gate, adapter, FakeGateway(adapter), resolver


def call(gate, gw, event):
    return gate.pre_gateway_dispatch(event=event, gateway=gw, session_store=FakeStore())


def test_admitted_returns_none_no_send():
    gate, ad, gw, res = make(spent="0.1")
    assert call(gate, gw, Event()) is None
    assert ad.sent == []
    assert res.resolve(session_key="agent:main:discord:group:123") == "42"


def test_exceeded_skips_and_sends_with_thread():
    gate, ad, gw, _ = make()
    ev = Event(source=src(thread_id="999"))
    assert call(gate, gw, ev) == {"action": "skip", "reason": "discord_quota_exceeded"}
    assert len(ad.sent) == 1
    chat, text, meta = ad.sent[0]
    assert chat == "123" and meta == {"thread_id": "999"}
    assert "Kvoten är slut" in text


def test_blocked_text():
    gate, ad, gw, _ = make(daily=0, spent="0")
    assert call(gate, gw, Event())["action"] == "skip"
    assert "Åtkomst blockerad" in ad.sent[0][1]


def test_blocked_and_other_exhausted_still_blocked_text():
    gate, ad, gw, _ = make(daily=0, weekly=1.0, spent="9")
    call(gate, gw, Event())
    assert "Åtkomst blockerad" in ad.sent[0][1]


def test_bypass_commands_even_over_budget():
    gate, ad, gw, _ = make()
    for cmd in ("/quota", "/help", "/whoami", "/stop now"):
        assert call(gate, gw, Event(cmd)) is None
    assert ad.sent == []
    assert call(gate, gw, Event("/other"))["action"] == "skip"


def test_failing_send_logged_and_skip(caplog):
    gate, ad, gw, _ = make(adapter=FakeAdapter(fail=True))
    assert call(gate, gw, Event())["action"] == "skip"
    assert "Failed to deliver" in caplog.text


def test_adapter_lookup_failure_still_skips():
    gate, ad, _, _ = make()
    bad = SimpleNamespace(_adapter_for_source=lambda s: 1 / 0)
    assert call(gate, bad, Event())["action"] == "skip"


def test_send_scheduled_on_running_loop():
    gate, ad, gw, _ = make()

    async def run():
        r = call(gate, gw, Event())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return r

    assert asyncio.run(run())["action"] == "skip"
    assert len(ad.sent) == 1


def test_disabled_noop():
    gate, ad, gw, res = make(enabled=False)
    assert call(gate, gw, Event()) is None
    assert ad.sent == [] and res.size() == 0


def test_ignored_events():
    gate, ad, gw, _ = make()
    assert call(gate, gw, Event(source=src(platform=SimpleNamespace(value="telegram")))) is None
    assert call(gate, gw, Event(internal=True)) is None
    assert call(gate, gw, Event(source=src(is_bot=True))) is None
    assert call(gate, gw, Event(source=src(user_id=""))) is None
    assert call(gate, gw, Event(source=src(user_id=None))) is None
    assert ad.sent == []


def test_ledger_error_fails_closed_with_reply():
    gate, ad, gw, _ = make(ledger=FakeLedger(boom=True))
    assert call(gate, gw, Event()) == {"action": "skip", "reason": "discord_quota_unavailable"}
    assert len(ad.sent) == 1 and "tillfälligt otillgänglig" in ad.sent[0][1]


def test_config_error_fails_closed_but_bypass_and_non_discord_pass():
    def boom():
        raise RuntimeError("cfg")
    ad = FakeAdapter()
    gate = AdmissionGate(boom, FakeLedger(), UserResolver(context_getter=lambda: ("", "")))
    gw = FakeGateway(ad)
    assert call(gate, gw, Event())["reason"] == "discord_quota_unavailable"
    assert call(gate, gw, Event("/quota")) is None
    assert call(gate, gw, Event(source=src(platform=SimpleNamespace(value="telegram")))) is None
    assert call(gate, gw, Event(source=src(is_bot=True))) is None
    assert call(gate, gw, Event(internal=True)) is None


def test_unavailable_warning_rate_limited(caplog):
    gate, ad, gw, _ = make(ledger=FakeLedger(boom=True))
    for _ in range(3):
        call(gate, gw, Event())
    assert caplog.text.count("fail closed") == 1


def test_garbage_event_never_escapes():
    gate, ad, gw, _ = make()
    assert gate.pre_gateway_dispatch(event=object(), gateway=None) is None
    assert gate.pre_gateway_dispatch() is None


def test_failed_send_result_logged(caplog):
    class Bad(FakeAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SimpleNamespace(success=False, error="403 forbidden")
    gate, ad, gw, _ = make(adapter=Bad())
    assert call(gate, gw, Event())["action"] == "skip"
    assert "403 forbidden" in caplog.text


def test_ttl_config_getter():
    from discord_quota.plugin import _ttl_config_getter
    t = [0.0]
    n = [0]
    def loader():
        n[0] += 1
        return n[0]
    g = _ttl_config_getter(loader, ttl=30, clock=lambda: t[0])
    assert (g(), g()) == (1, 1)
    t[0] = 31
    assert g() == 2


def test_binding_hooks():
    gate, _, _, res = make()
    gate.pre_llm_call(session_id="s1", turn_id="t1", platform="discord", sender_id="7")
    gate.subagent_start(child_session_id="c1", parent_session_id="s1", parent_turn_id="t1")
    assert res.resolve(turn_id="t1") == "7"
    assert res.resolve(session_id="c1") == "7"
    gate.pre_llm_call(session_id=None, turn_id=None, platform=None, sender_id=None)
    gate.subagent_start()


def test_register_wires_hooks(fake_ctx):
    from discord_quota import register
    register(fake_ctx)
    assert {"pre_gateway_dispatch", "pre_llm_call", "subagent_start"} <= set(fake_ctx.hook_names())


def test_manifest_lists_registered_hooks(fake_ctx):
    from pathlib import Path
    from discord_quota import register
    register(fake_ctx)
    import yaml
    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text())
    # Hermes reads `provides_hooks`; the legacy `hooks` key is ignored by plugin doctor.
    assert set(fake_ctx.hook_names()) <= set(manifest["provides_hooks"])


# -- concurrency guard (#9) ------------------------------------------------
import threading

from discord_quota.admission import CONCURRENT_REASON, MARKER_GRACE_SECONDS


class KeyedStore:
    def _generate_session_key(self, source):
        return f"agent:main:discord:group:{source.chat_id}"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def two_chan(gate, gw, text="hi", user="42"):
    out = []
    for chat in ("111", "222"):
        ev = Event(text, source=src(chat_id=chat, user_id=user))
        out.append(gate.pre_gateway_dispatch(event=ev, gateway=gw, session_store=KeyedStore()))
    return out


def test_second_channel_rejected_when_low_balance():
    gate, ad, gw, _ = make(daily=1.0, spent="0.95")
    first, second = two_chan(gate, gw)
    assert first is None
    assert second["reason"] == CONCURRENT_REASON
    assert len(ad.sent) == 1 and "0,05 USD" in ad.sent[0][1]


def test_second_channel_admitted_with_budget():
    gate, ad, gw, _ = make(daily=1.0, spent="0.5")
    assert two_chan(gate, gw) == [None, None]
    assert ad.sent == []


def test_exact_threshold_admitted():
    gate, _, gw, _ = make(daily=1.0, spent="0.90")  # remaining 0.10, not < 0.10
    assert two_chan(gate, gw) == [None, None]


def test_unlimited_never_guarded():
    gate, _, gw, _ = make(daily=-1, weekly=-1, spent="999")
    assert two_chan(gate, gw) == [None, None]
    assert gate.inflight_count("42") == 0


def test_same_channel_not_parallel():
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    ev = lambda: Event("x", source=src(chat_id="111"))
    assert gate.pre_gateway_dispatch(event=ev(), gateway=gw, session_store=KeyedStore()) is None
    assert gate.pre_gateway_dispatch(event=ev(), gateway=gw, session_store=KeyedStore()) is None


def test_other_users_independent():
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    a = gate.pre_gateway_dispatch(event=Event(source=src(chat_id="1", user_id="1")),
                                  gateway=gw, session_store=KeyedStore())
    b = gate.pre_gateway_dispatch(event=Event(source=src(chat_id="2", user_id="2")),
                                  gateway=gw, session_store=KeyedStore())
    assert a is None and b is None


def test_on_session_end_clears_marker():
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    gate.pre_gateway_dispatch(event=Event(source=src(chat_id="111")), gateway=gw,
                              session_store=KeyedStore())
    gate.pre_llm_call(session_id="s1", turn_id="t1", platform="discord", sender_id="42")
    assert gate.inflight_count("42") == 1
    gate.on_session_end(session_id="s1", turn_id="t1")
    assert gate.inflight_count("42") == 0
    ev = Event(source=src(chat_id="222"))
    assert gate.pre_gateway_dispatch(event=ev, gateway=gw, session_store=KeyedStore()) is None


def test_stale_marker_cleared_when_agent_gone():
    clock = Clock()
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    gate._mono = clock
    gw._running_agents = {"agent:main:discord:group:111": object()}
    first = gate.pre_gateway_dispatch(event=Event(source=src(chat_id="111")), gateway=gw,
                                      session_store=KeyedStore())
    assert first is None
    clock.t += MARKER_GRACE_SECONDS + 1
    ev = Event(source=src(chat_id="222"))
    # agent still running: rejected
    assert gate.pre_gateway_dispatch(event=ev, gateway=gw,
                                     session_store=KeyedStore())["reason"] == CONCURRENT_REASON
    gw._running_agents.clear()  # crash: agent gone
    assert gate.pre_gateway_dispatch(event=ev, gateway=gw, session_store=KeyedStore()) is None


def test_marker_expires_without_running_agents_map():
    clock = Clock()
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    gate._mono = clock
    gate.pre_gateway_dispatch(event=Event(source=src(chat_id="111")), gateway=gw,
                              session_store=KeyedStore())
    clock.t += 901
    assert gate.pre_gateway_dispatch(event=Event(source=src(chat_id="222")), gateway=gw,
                                     session_store=KeyedStore()) is None


def test_thread_safe_exactly_one_admitted():
    gate, _, gw, _ = make(daily=1.0, spent="0.95")
    results, barrier = [], threading.Barrier(8)

    def run(i):
        ev = Event(source=src(chat_id=str(i)))
        barrier.wait()
        results.append(gate.pre_gateway_dispatch(event=ev, gateway=gw, session_store=KeyedStore()))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(r is None for r in results) == 1


def _admit(gate, gw, chat, user="42"):
    return gate.pre_gateway_dispatch(event=Event(source=src(chat_id=chat, user_id=user)),
                                     gateway=gw, session_store=KeyedStore())


def test_attach_by_session_key_not_oldest_marker():
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    _admit(gate, gw, "111")
    _admit(gate, gw, "222")
    key_b = "agent:main:discord:group:222"
    gate._context_session_key = lambda: key_b
    gate.pre_llm_call(session_id="sB", turn_id="tB", platform="discord", sender_id="42")
    gate.on_session_end(session_id="sB")
    # channel 222 released, channel 111 still guarded
    assert gate.inflight_count("42") == 1
    assert list(gate._inflight["42"]) == ["agent:main:discord:group:111"]


def test_attach_ambiguous_without_key_is_skipped():
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    _admit(gate, gw, "111")
    _admit(gate, gw, "222")
    gate._context_session_key = lambda: ""
    gate.pre_llm_call(session_id="sX", turn_id="t", platform="discord", sender_id="42")
    gate.on_session_end(session_id="sX")
    assert gate.inflight_count("42") == 2


def test_attach_single_marker_without_key():
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    _admit(gate, gw, "111")
    gate._context_session_key = lambda: ""
    gate.pre_llm_call(session_id="s1", turn_id="t", platform="discord", sender_id="42")
    gate.on_session_end(session_id="s1")
    assert gate.inflight_count("42") == 0


def test_readmission_same_key_keeps_session_id():
    clock = Clock()
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    gate._mono = clock
    gate._context_session_key = lambda: "agent:main:discord:group:111"
    _admit(gate, gw, "111")
    gate.pre_llm_call(session_id="s1", turn_id="t", platform="discord", sender_id="42")
    clock.t += 5
    _admit(gate, gw, "111")  # queued second message, same session
    marker = gate._inflight["42"]["agent:main:discord:group:111"]
    assert marker[1] == "s1" and marker[0] == 1000.0
    gate.on_session_end(session_id="s1")
    assert gate.inflight_count("42") == 0


def test_readmission_refreshes_unattached_timestamp():
    clock = Clock()
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    gate._mono = clock
    _admit(gate, gw, "111")
    clock.t += 5
    _admit(gate, gw, "111")
    assert gate._inflight["42"]["agent:main:discord:group:111"][0] == 1005.0


def test_thread_safe_interleaved_attach_release():
    gate, _, gw, _ = make(daily=1.0, spent="0.5")
    for i in range(8):
        _admit(gate, gw, str(i))
    errors = []

    def run(i):
        try:
            gate._attach_session("42", f"s{i}", f"agent:main:discord:group:{i}")
            gate.on_session_end(session_id=f"s{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and gate.inflight_count("42") == 0
