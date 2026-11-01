"""Load the plugin the way Hermes does (a package from its directory), with a scratch Hermes home."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Ctx:
    """The parts of Hermes's plugin context this plugin uses, keeping what it registered."""

    def __init__(self):
        self.tools, self.hooks, self.commands = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **_):
        self.tools[name] = handler

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_cli_command(self, name, **kwargs):
        self.commands[name] = kwargs


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    constants = type(sys)("hermes_constants")
    constants.get_hermes_home = lambda: Path(state["home"])
    state = {"home": home}
    constants.state = state
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    return state


@pytest.fixture
def plugin(home, monkeypatch):
    name = "agent_secrets_under_test"
    for loaded in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        monkeypatch.delitem(sys.modules, loaded)
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    ctx = Ctx()
    monkeypatch.delitem(sys.modules, module.API_MODULE, raising=False)
    module.register(ctx)
    monkeypatch.setattr(module, "POLL", 0.05)
    module.ctx = ctx
    return module
