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
    assert "Quota Exceeded" in text


def test_blocked_text():
    gate, ad, gw, _ = make(daily=0, spent="0")
    assert call(gate, gw, Event())["action"] == "skip"
    assert "Access Blocked" in ad.sent[0][1]


def test_blocked_and_other_exhausted_still_blocked_text():
    gate, ad, gw, _ = make(daily=0, weekly=1.0, spent="9")
    call(gate, gw, Event())
    assert "Access Blocked" in ad.sent[0][1]


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


def test_exception_never_escapes():
    gate, ad, gw, _ = make(ledger=FakeLedger(boom=True))
    assert call(gate, gw, Event()) is None
    assert gate.pre_gateway_dispatch(event=object(), gateway=None) is None
    assert gate.pre_gateway_dispatch() is None


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
    text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
    for name in fake_ctx.hook_names():
        assert f"  - {name}\n" in text
