import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_register_with_fake_ctx(fake_ctx, load_plugin_package):
    module = load_plugin_package()
    module.register(fake_ctx)  # must not raise


def test_inner_package_register(fake_ctx):
    from discord_quota import register
    register(fake_ctx)


def test_plugin_manifest_present():
    assert (REPO_ROOT / "plugin.yaml").is_file()
    assert (REPO_ROOT / "__init__.py").is_file()


def test_import_does_not_need_hermes():
    code = ("import sys; sys.path.insert(0, %r); import discord_quota; "
            "bad=[m for m in sys.modules if m.split('.')[0] in "
            "('hermes_cli','gateway','agent','run_agent')]; assert not bad, bad"
            % str(REPO_ROOT))
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_register_wires_free_providers(monkeypatch, tmp_path, fake_ctx):
    """register() must hand the config getter to the recorder and subagent hooks."""
    from decimal import Decimal

    from discord_quota import config, ledger, plugin, recorder, resolver, subagent

    cfg = config.parse_config({"enabled": True, "free_providers": ["vertex"]})
    db = tmp_path / "q.db"
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(ledger, "default_db_path", lambda: db)
    monkeypatch.setattr(recorder, "_default_estimator", lambda *a: (Decimal("1"), "estimated"))
    monkeypatch.setattr(subagent, "_default_cost_lookup",
                        lambda cid: (Decimal("1"), "m", "vertex" if cid == "cv" else "deepseek"))

    class R:
        def resolve(self, **kw):
            return "42"
    monkeypatch.setattr(resolver, "UserResolver", R)
    plugin.register(fake_ctx)
    hooks = dict(fake_ctx.hooks)
    for prov in ("vertex", "deepseek"):
        hooks["post_api_request"](session_id="s-" + prov, turn_id="t", platform="discord",
                                  model="m", provider=prov, usage={})
    hooks["subagent_stop"](parent_session_id="p", parent_turn_id="t", child_session_id="cv")
    hooks["subagent_stop"](parent_session_id="p", parent_turn_id="t", child_session_id="cd")
    led = ledger.Ledger(db)
    cost = {r.session_id: r.cost_usd for r in led.recent("42")}
    assert cost["s-vertex"] == 0 and cost["s-deepseek"] == Decimal("1")
    assert cost["cv"] == 0 and cost["cd"] == Decimal("1")
