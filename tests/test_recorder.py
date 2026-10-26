import logging
from decimal import Decimal

from discord_quota.ledger import Ledger
from discord_quota.recorder import SpendRecorder


class FakeResolver:
    def __init__(self, user="42"):
        self.user, self.calls = user, []

    def resolve(self, session_id=None, turn_id=None, session_key=None):
        self.calls.append((session_id, turn_id))
        return self.user


class FakeLedger:
    def __init__(self, boom=False):
        self.rows, self.boom = [], boom

    def record(self, user, session, cost, model, provider, **kw):
        if self.boom:
            raise RuntimeError("db")
        self.rows.append((user, session, cost, model, provider, kw))


def est(prices):
    def f(model, usage, provider, base_url):
        if model not in prices:
            return None, "unknown"
        return Decimal(prices[model]), "estimated"
    return f


KW = dict(session_id="s1", turn_id="t1", platform="subagent", model="m", provider="p",
          usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3})


def test_records_discord_call():
    led = FakeLedger()
    SpendRecorder(led, FakeResolver(), est({"m": "0.12345678"})).post_api_request(**KW)
    user, sess, cost, model, prov, kw = led.rows[0]
    assert (user, sess, model, prov) == ("42", "s1", "m", "p")
    assert cost == Decimal("0.123457")
    assert kw == {"turn_id": "t1", "input_tokens": 13, "output_tokens": 5}


def test_non_discord_ignored():
    led = FakeLedger()
    SpendRecorder(led, FakeResolver(None), est({"m": "1"})).post_api_request(**KW)
    assert led.rows == []


def test_unpriced_records_zero_and_warns(caplog):
    led = FakeLedger()
    with caplog.at_level(logging.WARNING):
        SpendRecorder(led, FakeResolver(), est({})).post_api_request(**KW)
    assert led.rows[0][2] == Decimal(0)
    assert "Unpriced model usage: m on p - accruing $0.00" in caplog.text


def test_moa_references_included():
    led = FakeLedger()
    refs = [{"model": "a", "provider": "p", "usage": {}},
            {"model": "unk", "provider": "p", "usage": {}, "cost_usd": "0.5"}]
    SpendRecorder(led, FakeResolver(), est({"m": "1", "a": "0.25"})).post_api_request(
        **KW, moa_references=refs)
    assert led.rows[0][2] == Decimal("1.75")


def test_errors_never_raise(caplog):
    def bad(*a):
        raise RuntimeError("pricing")
    SpendRecorder(FakeLedger(), FakeResolver(), bad).post_api_request(**KW)  # priced as 0
    SpendRecorder(FakeLedger(boom=True), FakeResolver(), est({"m": "1"})).post_api_request(**KW)
    class R:
        def resolve(self, **k):
            raise RuntimeError("x")
    SpendRecorder(FakeLedger(), R()).post_api_request(**KW)


def test_missing_usage_and_real_ledger(tmp_path):
    led = Ledger(tmp_path / "q.db")
    SpendRecorder(led, FakeResolver(), est({"m": "0.5"})).post_api_request(
        session_id="s", turn_id="t", model="m", provider="p", usage=None)
    assert led.spend_since("42", 0) == Decimal("0.5")


def test_registered(fake_ctx, tmp_path, monkeypatch):
    from discord_quota import plugin
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin.register(fake_ctx)
    assert "post_api_request" in fake_ctx.hook_names()


def test_moa_references_charged_once_per_turn():
    led = FakeLedger()
    refs = [{"model": "a", "provider": "p", "usage": {}}]
    rec = SpendRecorder(led, FakeResolver(), est({"m": "1", "a": "0.25"}))
    rec.post_api_request(**KW, moa_references=refs)
    rec.post_api_request(**KW, moa_references=list(refs))
    assert [r[2] for r in led.rows] == [Decimal("1.25"), Decimal("1")]
    rec.post_api_request(**{**KW, "turn_id": "t2"}, moa_references=refs)
    assert led.rows[2][2] == Decimal("1.25")


def test_moa_dedupe_state_bounded():
    rec = SpendRecorder(FakeLedger(), FakeResolver(), est({"m": "1"}))
    rec._max_turns = 3
    for i in range(10):
        rec.post_api_request(**{**KW, "turn_id": f"t{i}"}, moa_references=[{"model": "a"}])
    assert len(rec._seen_refs) == 3
