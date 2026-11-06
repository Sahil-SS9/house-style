"""A fake Hermes host for the adapter.

Everything the adapter needs from Hermes now comes through ``ctx`` (``redact_text``,
``session_cwd``, ``dispatch_tool`` and ``llm``) plus the host's plugin-data directory,
so this harness is small on purpose: if an adapter change needs a host module here, it
has reached into Hermes internals and should not have.
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
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, **kwargs) -> FakeLlmResult:
        self.calls.append({"messages": messages, **kwargs})
        return FakeLlmResult(self.reply)


class FakeCtx:
    """The documented plugin surface, as far as house-style uses it."""

    def __init__(
        self,
        *,
        reply: str = "",
        cwd: Path | None = None,
        redactor: Callable[[str], str] | None = None,
        remote: dict[str, str] | None = None,
    ) -> None:
        self.hooks: dict[str, Callable] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.llm = FakeLlm(reply)
        self._cwd = str(cwd or "")
        self.redactor = redactor
        self.remote = dict(remote or {})
        self.redact_calls: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []

    # -- hooks, commands, model -----------------------------------------
    def register_hook(self, name, callback, **_kwargs):
        self.hooks[name] = callback
        return None

    def register_command(self, name, handler, description="", args_hint="", **_kwargs):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}
        return None

    # -- session ---------------------------------------------------------
    def session_cwd(self) -> str:
        return self._cwd

    def set_cwd(self, path: Path) -> None:
        self._cwd = str(path)

    # -- redaction -------------------------------------------------------
    def redact_text(self, text: str) -> str:
        self.redact_calls.append(text)
        if self.redactor is not None:
            return self.redactor(text)
        return text

    # -- tool dispatch ---------------------------------------------------
    def dispatch_tool(self, tool_name: str, args: dict, **_kwargs) -> str:
        self.tool_calls.append((tool_name, args))
        if tool_name != "read_file":
            return ""
        text = self.remote.get(str(args.get("path")))
        if text is None:
            return f"Error: no such file: {args.get('path')}"
        return "\n".join(f"{index}|{line}" for index, line in enumerate(text.splitlines(), start=1))


@dataclass
class FakeHost:
    home: Path
    repo: Path
    ctx: FakeCtx
    plugin: Any

    @property
    def data_dir(self) -> Path:
        return self.home / "plugin-data" / "house-style"


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    mod.__path__ = []
    return mod


def build_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str = "",
    redactor: Callable[[str], str] | None = None,
    remote: dict[str, str] | None = None,
    expose_redact: bool = True,
) -> tuple[FakeHost, FakeCtx]:
    home = tmp_path / "hermes-home"
    repo = tmp_path / "project"
    (repo / "pkg").mkdir(parents=True)

    def plugin_data_dir(name: str) -> Path:
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setitem(sys.modules, "plugins", _module("plugins"))
    monkeypatch.setitem(
        sys.modules,
        "plugins.plugin_storage",
        _module("plugins.plugin_storage", plugin_data_dir=plugin_data_dir),
    )

    ctx = FakeCtx(reply=reply, cwd=repo, redactor=redactor, remote=remote)
    if not expose_redact:
        ctx.redact_text = None  # type: ignore[assignment]  # an older host
    module = importlib.reload(importlib.import_module("house_style_hermes.plugin"))
    module.register(ctx)
    return FakeHost(home=home, repo=repo, ctx=ctx, plugin=module), ctx


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    built, _ = build_host(tmp_path, monkeypatch)
    return built


@pytest.fixture()
def host_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(**kwargs) -> FakeHost:
        return build_host(tmp_path, monkeypatch, **kwargs)[0]

    return build
