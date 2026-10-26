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
