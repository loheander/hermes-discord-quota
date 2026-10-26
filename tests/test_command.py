from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from discord_quota.command import DESCRIPTION, NOT_DISCORD_REPLY, QuotaCommand
from discord_quota.config import Limits, QuotaConfig
from discord_quota.resolver import UserResolver

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class Ledger:
    def __init__(self, spent="0.42", boom=False):
        self.spent, self.boom = Decimal(spent), boom

    def spend_since(self, user, start, end=None):
        if self.boom:
            raise RuntimeError("db")
        return self.spent


def make(limits=Limits(1.0, 5.0), enabled=True, **kw):
    cfg = QuotaConfig(enabled=enabled, default=limits)
    return QuotaCommand(lambda: cfg, Ledger(**kw), UserResolver(context_getter=lambda: ("", "")),
                        clock=lambda: NOW)


def event(platform="discord", user="42", bot=False):
    return SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform),
                                                   user_id=user, is_bot=bot))


def test_registered_with_fake_ctx(fake_ctx):
    from discord_quota import register
    register(fake_ctx)
    cmd = [c for c in fake_ctx.commands if c["name"] == "quota"]
    assert len(cmd) == 1 and cmd[0]["description"] == DESCRIPTION
    assert callable(cmd[0]["handler"])


def test_status_for_invoker():
    c = make()
    assert c.observe_dispatch(event()) is None
    out = c.handle("")
    assert "Kvotstatus för Discord" in out and "0,58 USD / 1,00 USD kvar" in out
    assert "Status: Aktiv" in out


def test_non_discord_explains():
    c = make()
    c.observe_dispatch(event(platform="telegram"))
    assert c.handle("") == NOT_DISCORD_REPLY


def test_no_invoker_explains():
    assert make().handle("") == NOT_DISCORD_REPLY


def test_resolver_preferred():
    c = make()
    c._resolver = SimpleNamespace(resolve=lambda **k: "7")
    assert "Kvotstatus för Discord" in c.handle("")


def test_exceeded_and_blocked_and_unlimited():
    c = make(spent="2")
    c.observe_dispatch(event())
    assert "Status: Överskriden" in c.handle("")
    c = make(limits=Limits(0.0, 5.0))
    c.observe_dispatch(event())
    assert "Status: Blockerad" in c.handle("")
    c = make(limits=Limits(-1.0, -1.0))
    c.observe_dispatch(event())
    out = c.handle("")
    assert "obegränsad" in out and "Status: Aktiv" in out


def test_error_returns_unavailable():
    c = make(boom=True)
    c.observe_dispatch(event())
    assert "tillfälligt otillgänglig" in c.handle("")


def test_bot_clears_invoker():
    c = make()
    c.observe_dispatch(event())
    c.observe_dispatch(event(bot=True))
    assert c.handle("") == NOT_DISCORD_REPLY


def test_swedish_constants():
    assert DESCRIPTION == "Visa din återstående kostnadskvot på Discord"
    assert NOT_DISCORD_REPLY == "/quota visar din kostnadskvot och fungerar bara när du skriver till boten på Discord."
