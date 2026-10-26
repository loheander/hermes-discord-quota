import json
import logging
from decimal import Decimal

from discord_quota.media import MediaSpendRecorder


class R:
    def __init__(self, user="42"):
        self.user = user

    def resolve(self, session_id=None, turn_id=None, session_key=None):
        return self.user


class L:
    def __init__(self):
        self.rows = []

    def record(self, user, session, cost, model, provider, **kw):
        self.rows.append((user, session, cost, model, provider, kw))


class Cfg:
    def __init__(self, free=()):
        self.free = free

    def is_free_provider(self, p):
        return p in self.free


def run(result, tool="image_generate", user="42", free=(), after=None):
    led = L()
    rec = MediaSpendRecorder(led, R(user), after_record=after, config_getter=lambda: Cfg(free))
    rec.post_tool_call(tool_name=tool, args={}, result=result, session_id="s", turn_id="t")
    return led.rows


def test_image_cost_usd():
    res = json.dumps({"success": True, "model": "m", "provider": "openrouter", "cost_usd": 0.04})
    (row,) = run(res)
    assert row[:5] == ("42", "s", Decimal("0.04"), "m", "openrouter")
    assert row[5] == {"turn_id": "t", "source_kind": "media"}


def test_video_extra_cost():
    res = json.dumps({"success": True, "model": "v", "provider": "openrouter", "extra": {"cost": 0.5}})
    assert run(res, "video_generate")[0][2] == Decimal("0.5")


def test_video_top_level_cost_dict_result():
    assert run({"success": True, "provider": "p", "cost": "1.2345678"}, "video_generate")[0][2] == Decimal("1.234568")


def test_free_provider_zero():
    res = {"success": True, "provider": "vertex", "cost_usd": 1}
    assert run(res, free=("vertex",))[0][2] == Decimal(0)


def test_errors_record_nothing():
    assert run({"success": False, "error": "x", "cost_usd": 1}) == []
    assert run({"success": True, "error": "x"}) == []
    assert run("not json") == []


def test_missing_cost_zero_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="hermes_discord_quota"):
        rows = run({"success": True, "provider": "fal", "model": "m"})
    assert rows[0][2] == Decimal(0)
    msgs = [r.getMessage() for r in caplog.records if "Unpriced media usage" in r.getMessage()]
    assert len(msgs) == 1 and "image_generate" in msgs[0] and "fal" in msgs[0]


def test_non_discord_and_other_tools_ignored():
    assert run({"success": True, "cost_usd": 1}, user=None) == []
    assert run({"success": True, "cost_usd": 1}, tool="web_search") == []


def test_after_record_fires():
    calls = []
    run({"success": True, "cost_usd": 1}, after=lambda *a: calls.append(a))
    assert calls == [("42", "s", "t", 1)]


def test_never_raises():
    class Boom(L):
        def record(self, *a, **k):
            raise RuntimeError("db")

    rec = MediaSpendRecorder(Boom(), R())
    rec.post_tool_call(tool_name="image_generate", result={"success": True, "cost_usd": 1})
