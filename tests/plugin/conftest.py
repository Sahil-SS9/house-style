"""A fake Hermes host, so the adapter can be driven without a Hermes checkout.

Every host module the adapter touches is replaced with a small stub in ``sys.modules``
(restored automatically after each test). The stubs model the documented seams: the
plugin-data directory, the redactor, the protected-path predicate, the agent working
directory, the tool-path resolver and ``VALID_HOOKS``.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 10
    total_tokens: int = 20
    cost_usd: float | None = 0.002


@dataclass
class FakeLlmResult:
    text: str
    provider: str = "fake"
    model: str = "fake-model"
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeLlm:
    """Records calls and returns a scripted reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, **kwargs) -> FakeLlmResult:
        self.calls.append({"messages": messages, **kwargs})
        return FakeLlmResult(self.reply)


class FakeCtx:
    def __init__(self, *, settings: dict[str, Any] | None = None, reply: str = "") -> None:
        self.hooks: dict[str, Callable] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, Any] = {}
        self.settings = settings or {}
        self.llm = FakeLlm(reply)

    def register_hook(self, name, callback, **_kwargs):
        self.hooks[name] = callback
        return None

    def register_command(self, name, handler, description="", args_hint="", **_kwargs):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}
        return None

    def register_tool(self, name, toolset, schema, handler, **_kwargs):
        self.tools[name] = handler
        return None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


@dataclass
class FakeHost:
    home: Path
    repo: Path
    ctx: FakeCtx
    plugin: Any
    redactor_calls: list[str]

    @property
    def data_dir(self) -> Path:
        return self.home / "plugin-data" / "house-style"


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    mod.__path__ = []  # treat as a package so submodule imports resolve
    return mod


def build_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reply: str = ""
) -> tuple[FakeHost, Callable]:
    """Install the stubs and import the plugin module fresh."""
    home = tmp_path / "hermes-home"
    repo = tmp_path / "project"
    (repo / "pkg").mkdir(parents=True)
    redactor_calls: list[str] = []
    state = {"cwd": repo}

    def plugin_data_dir(name: str) -> Path:
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def redact_sensitive_text(text: str, *, force: bool = False, **_kwargs) -> str:
        redactor_calls.append(text)
        return text.replace("HOSTSECRET", "[redacted-by-host]")

    def is_write_denied(path: str) -> bool:
        return Path(path).name == "blocked.py"

    def resolve_agent_cwd() -> Path:
        return state["cwd"]

    def resolve_path_for_task(raw: str, task_id: str = "default") -> str:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = state["cwd"] / raw
        return str(candidate)

    monkeypatch.setitem(sys.modules, "plugins", _module("plugins"))
    monkeypatch.setitem(
        sys.modules,
        "plugins.plugin_storage",
        _module("plugins.plugin_storage", plugin_data_dir=plugin_data_dir),
    )
    monkeypatch.setitem(sys.modules, "agent", _module("agent"))
    monkeypatch.setitem(
        sys.modules, "agent.redact", _module("agent.redact", redact_sensitive_text=redact_sensitive_text)
    )
    monkeypatch.setitem(
        sys.modules, "agent.file_safety", _module("agent.file_safety", is_write_denied=is_write_denied)
    )
    monkeypatch.setitem(
        sys.modules, "agent.runtime_cwd", _module("agent.runtime_cwd", resolve_agent_cwd=resolve_agent_cwd)
    )
    monkeypatch.setitem(sys.modules, "tools", _module("tools"))
    monkeypatch.setitem(
        sys.modules,
        "tools.file_tools_paths",
        _module("tools.file_tools_paths", _resolve_path_for_task=resolve_path_for_task),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli",
        _module("hermes_cli"),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        _module(
            "hermes_cli.plugins",
            VALID_HOOKS={"pre_tool_call", "post_tool_call", "pre_llm_call", "on_session_start"},
        ),
    )

    ctx = FakeCtx(reply=reply)
    module = importlib.reload(importlib.import_module("house_style_hermes.plugin"))
    module.register(ctx)
    host = FakeHost(home=home, repo=repo, ctx=ctx, plugin=module, redactor_calls=redactor_calls)
    return host, lambda path: state.__setitem__("cwd", path)


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    built, _ = build_host(tmp_path, monkeypatch)
    return built


@pytest.fixture()
def host_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(*, reply: str = "") -> tuple[FakeHost, Callable]:
        return build_host(tmp_path, monkeypatch, reply=reply)

    return build
