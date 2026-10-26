"""Shared fixtures: a fake PluginContext and a loader mimicking Hermes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakePluginContext:
    """Records registrations made through the PluginContext API."""

    def __init__(self) -> None:
        self.hooks: list[tuple[str, Callable]] = []
        self.commands: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append((hook_name, callback))

    def register_command(self, name: str, handler: Callable, description: str = "",
                         args_hint: str = "", argument_mode: str | None = None) -> None:
        self.commands.append({"name": name, "handler": handler, "description": description,
                              "args_hint": args_hint, "argument_mode": argument_mode})

    def register_cli_command(self, name: str, help: str, setup_fn: Callable,
                             handler_fn: Callable | None = None, description: str = "") -> None:
        self.cli_commands.append({"name": name, "help": help, "setup_fn": setup_fn,
                                  "handler_fn": handler_fn, "description": description})

    def hook_names(self) -> list[str]:
        return [name for name, _ in self.hooks]


@pytest.fixture
def fake_ctx() -> FakePluginContext:
    return FakePluginContext()


@pytest.fixture
def load_plugin_package():
    """Import the repo root as a package the way Hermes does (plugins_loader.py)."""
    name = "hermes_plugins_test_discord_quota"

    def _load():
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(
            name, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    yield _load
    sys.modules.pop(name, None)
    for key in [k for k in sys.modules if k.startswith(name + ".")]:
        sys.modules.pop(key)
