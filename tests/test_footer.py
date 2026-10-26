from datetime import datetime, timezone
from decimal import Decimal

from discord_quota.config import Limits, QuotaConfig
from discord_quota.footer import FooterHook
from discord_quota.resolver import UserResolver

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class Ledger:
    def __init__(self, spent="0", boom=False):
        self.spent, self.boom = Decimal(spent), boom

    def spend_since(self, user, start, end=None):
        if self.boom:
            raise RuntimeError("db")
        return self.spent


def make(spent, user="42", enabled=True, boom=False):
    cfg = QuotaConfig(enabled=enabled, default=Limits(1.0, 5.0))
    r = UserResolver(context_getter=lambda: ("", ""))
    if user:
        r.bind_turn("t1", "discord", user)
    return FooterHook(lambda: cfg, Ledger(spent, boom), r, clock=lambda: NOW)


def run(h, text="Hello\n\n"):
    return h.transform_llm_output(response_text=text, session_id="s", turn_id="t1", platform="discord")


def test_below_threshold_none():
    assert run(make("0.5")) is None


def test_warning_at_90():
    out = run(make("0.92"))
    assert out.startswith("Hello\n\n") and "Quota Warning" in out and "92%" in out
    assert out == run(make("0.92"))


def test_exhausted_at_100():
    out = run(make("1.04"))
    assert "Quota Exhausted" in out and out.startswith("Hello\n\n")


def test_empty_and_non_discord_untouched():
    assert run(make("2"), "  ") is None
    assert run(make("2", user=None)) is None


def test_disabled_none():
    assert run(make("2", enabled=False)) is None


def test_error_unchanged():
    assert run(make("2", boom=True)) is None


def test_registered():
    from discord_quota.plugin import register

    seen = []

    class Ctx:
        def register_hook(self, name, fn):
            seen.append(name)

        def register_command(self, *a, **k):
            pass

    register(Ctx())
    assert "transform_llm_output" in seen
