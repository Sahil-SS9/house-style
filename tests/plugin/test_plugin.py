"""The adapter: registration, the two hooks, and the command surface."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import FakeHost

from house_style_core import parse_patch

MODULE = "import os\n\n\ndef config_path(root):\n    return os.path.join(root, 'config.json')\n"
PATCHED = "from pathlib import Path\n\n\ndef config_path(root):\n    return Path(root) / 'config.json'\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


def make_repo(host: FakeHost) -> FakeHost:
    git(host.repo, "init", "-q", "-b", "main")
    (host.repo / "README.md").write_text("# project\n", encoding="utf-8")
    git(host.repo, "add", "-A")
    git(host.repo, "commit", "-q", "-m", "initial")
    return host


def write_file(host: FakeHost, rel: str, text: str, *, status: str = "ok") -> None:
    path = host.repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    host.ctx.hooks["post_tool_call"](
        tool_name="write_file",
        args={"path": rel, "content": text},
        status=status,
        task_id="local",
        session_id="s1",
        turn_id="t1",
    )


def patch_file(host: FakeHost, rel: str, old: str, new: str, *, replace_all: bool = False) -> None:
    path = host.repo / rel
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
    host.ctx.hooks["post_tool_call"](
        tool_name="patch",
        args={"path": rel, "old_string": old, "new_string": new, "replace_all": replace_all},
        status="ok",
        task_id="local",
        session_id="s1",
        turn_id="t1",
    )


def v4a_patch(host: FakeHost, patch_text: str, *, task_id: str = "local") -> None:
    # Apply for real, the way the host would, so capture reads the landed result.
    for hunk in parse_patch(patch_text).hunks:
        path = host.repo / hunk.path
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if hunk.kind == "add":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(hunk.added) + "\n", encoding="utf-8")
            continue
        if hunk.removed:
            start = _find_block(lines, hunk.removed)
            lines[start : start + len(hunk.removed)] = hunk.added
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    host.ctx.hooks["post_tool_call"](
        tool_name="patch",
        args={"mode": "patch", "patch": patch_text},
        status="ok",
        task_id=task_id,
        session_id="s1",
        turn_id="t1",
    )


def _find_block(lines: list[str], block: list[str]) -> int:
    for index in range(len(lines) - len(block) + 1):
        if lines[index : index + len(block)] == block:
            return index
    raise AssertionError(f"block not found: {block}")


def run(host: FakeHost, args: str = "") -> str:
    return host.ctx.commands["house-style"]["handler"](args)


# -- registration -----------------------------------------------------------
def test_registers_two_hooks_and_one_command(host: FakeHost):
    assert set(host.ctx.hooks) == {"post_tool_call", "on_session_start"}
    assert set(host.ctx.commands) == {"house-style"}


def test_the_injection_hook_is_gone(host: FakeHost):
    assert not hasattr(host.plugin, "on_pre_llm_call")
    assert not hasattr(host.plugin, "_inject_mode")


def test_registration_writes_nothing(host: FakeHost):
    assert not host.data_dir.exists(), "register() must not create the data directory"


def test_registration_imports_no_hermes_internals(host: FakeHost):
    source = Path(host.plugin.__file__).read_text(encoding="utf-8")
    for banned in ("from agent.", "import agent", "from hermes_cli.", "from tools."):
        assert banned not in source, f"adapter must use ctx only, found: {banned}"


# -- capture ----------------------------------------------------------------
def test_write_file_is_captured_and_reads_as_kept(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    status = run(host, "status")
    assert "signals: 1" in status
    assert "1 kept" in status


def test_other_tools_and_failed_writes_are_ignored(host: FakeHost):
    make_repo(host)
    host.ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "ls"}, status="ok")
    write_file(host, "pkg/module.py", "value = 1\n", status="error")
    assert "signals: 0" in run(host, "status")


def test_writing_the_learned_block_does_not_feed_back_into_capture(host_factory):
    host = host_factory(reply="1. Prefer pathlib (1 edit)\n")
    make_repo(host)
    write_file(host, "pkg/module.py", MODULE)
    (host.repo / "pkg/module.py").write_text(PATCHED, encoding="utf-8")
    run(host, "scan")
    run(host, "learn")
    run(host, "learn confirm")
    # The confirm wrote AGENTS.md itself; no tool call happened, so no signal was added.
    assert "signals: 1" in run(host, "status")


def test_patch_records_the_replacement_block(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/app.py", "def run():\n    return 1\n")
    patch_file(host, "pkg/app.py", "    return 1", "    return 2")
    assert "signals: 2" in run(host, "status")


def test_replace_all_captures_every_occurrence(host: FakeHost):
    make_repo(host)
    text = "def a():\n    return old()\n\ndef b():\n    return old()\n"
    write_file(host, "pkg/two.py", text)
    patch_file(host, "pkg/two.py", "    return old()", "    return new()", replace_all=True)
    status = run(host, "status")
    assert "signals: 3" in status, status


def test_a_multi_file_v4a_patch_captures_every_file(host: FakeHost):
    make_repo(host)
    (host.repo / "pkg/a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (host.repo / "pkg/b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    v4a_patch(
        host,
        "*** Begin Patch\n"
        "*** Update File: pkg/a.py\n"
        "-    return 1\n"
        "+    return 10\n"
        "*** Update File: pkg/b.py\n"
        "-    return 2\n"
        "+    return 20\n"
        "*** Add File: pkg/c.py\n"
        "+value = 3\n"
        "*** End Patch",
    )
    status = run(host, "status")
    assert "signals: 3" in status, status


def test_a_remote_backend_is_read_through_the_tool_surface(host_factory):
    host = host_factory(remote={"/workspace/pkg/far.py": "value = 1\n"})
    make_repo(host)
    (host.repo / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    host.ctx.hooks["post_tool_call"](
        tool_name="write_file",
        args={"path": "/workspace/pkg/far.py", "content": "value = 1\n"},
        status="ok",
        task_id="docker-3",
        session_id="s1",
    )
    status = run(host, "status")
    assert host.ctx.tool_calls and host.ctx.tool_calls[0][0] == "read_file"
    assert "signals: 1 (tool-surface 1)" in status, status


def test_an_unreadable_backend_is_named_in_status(host: FakeHost):
    make_repo(host)
    host.ctx.hooks["post_tool_call"](
        tool_name="write_file",
        args={"path": "/workspace/pkg/nope.py", "content": "value = 1\n"},
        status="ok",
        task_id="docker-9",
    )
    status = run(host, "status")
    assert "could not read on backend docker-9" in status, status


def test_redaction_goes_through_the_host(host_factory):
    host = host_factory(redactor=lambda text: text.replace("HOSTSECRET", "[redacted]"))
    make_repo(host)
    write_file(host, "pkg/settings.py", "token = HOSTSECRET\nvalue = 1\n")
    assert host.ctx.redact_calls, "ctx.redact_text must be called"
    stored = (host.data_dir / "signals").glob("*.jsonl")
    rows = "".join(path.read_text(encoding="utf-8") for path in stored)
    assert "HOSTSECRET" not in rows


def test_capture_is_refused_on_a_host_without_redact_text(host_factory):
    host = host_factory(expose_redact=False)
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    status = run(host, "status")
    assert "signals: 0" in status
    assert "capture: OFF" in status
    assert "ctx.redact_text()" in status
    assert "nothing was learned" in run(host, "learn")
    assert "nothing was scanned" in run(host, "scan")


# -- session context ---------------------------------------------------------
def test_the_session_cwd_is_used_not_the_process_cwd(host: FakeHost, tmp_path: Path, monkeypatch):
    make_repo(host)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    host.ctx.set_cwd(elsewhere)
    monkeypatch.setattr(host.plugin, "find_repo_root", lambda _path: None)
    # The process cwd during tests is a git repository; only the session cwd is elsewhere,
    # so this refusal proves the adapter reads ctx.session_cwd() rather than os.getcwd().
    assert "not inside a git repository" in run(host, "status")


# -- learn ------------------------------------------------------------------
def test_learn_stages_into_the_context_file_and_confirm_writes_it(host_factory):
    host = host_factory(reply="1. Prefer pathlib over os.path (1 edit)\n")
    make_repo(host)
    write_file(host, "pkg/module.py", MODULE)
    (host.repo / "pkg/module.py").write_text(PATCHED, encoding="utf-8")
    run(host, "scan")
    out = run(host, "learn")
    assert "Staged 1 rule" in out and "AGENTS.md" in out
    assert host.ctx.llm.calls
    assert not (host.repo / "AGENTS.md").exists(), "learn alone must not write"
    confirm = run(host, "learn confirm")
    assert "Wrote AGENTS.md" in confirm
    text = (host.repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Prefer pathlib over os.path (1 edit)" in text


def test_learn_writes_into_an_existing_hermes_md(host_factory):
    host = host_factory(reply="1. Prefer pathlib (1 edit)\n")
    make_repo(host)
    (host.repo / ".hermes.md").write_text("# Rules\n\nBe terse.\n", encoding="utf-8")
    write_file(host, "pkg/module.py", MODULE)
    (host.repo / "pkg/module.py").write_text(PATCHED, encoding="utf-8")
    run(host, "scan")
    run(host, "learn")
    run(host, "learn confirm")
    text = (host.repo / ".hermes.md").read_text(encoding="utf-8")
    assert "Prefer pathlib (1 edit)" in text and "Be terse." in text
    assert not (host.repo / "AGENTS.md").exists()


def test_learn_with_nothing_to_learn_from_does_not_call_the_model(host_factory):
    host = host_factory(reply="1. Rule (1 edit)\n")
    make_repo(host)
    assert "Nothing to learn from yet" in run(host, "learn")
    assert host.ctx.llm.calls == []


def test_confirm_without_a_proposal_says_so(host: FakeHost):
    make_repo(host)
    assert "No proposal is waiting" in run(host, "learn confirm")


def test_learn_reports_an_unusable_model_reply(host_factory):
    host = host_factory(reply="I could not find a pattern.")
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    (host.repo / "pkg/module.py").write_text("value = 2\n", encoding="utf-8")
    run(host, "scan")
    assert "no usable rules" in run(host, "learn")


# -- reset, off/on, export, usage -------------------------------------------
def test_off_stops_capture_and_on_resumes_it(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    assert "house-style is off for project" in run(host, "off")
    write_file(host, "pkg/other.py", "value = 2\n")
    assert "signals: 1" in run(host, "status")
    run(host, "on")
    write_file(host, "pkg/third.py", "value = 3\n")
    assert "signals: 2" in run(host, "status")


def test_reset_is_two_stage(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    assert "Would clear 1 stored edit" in run(host, "reset")
    assert "signals: 1" in run(host, "status")
    assert "Cleared 1 stored edit" in run(host, "reset confirm")
    assert "signals: 0" in run(host, "status")


def test_export_prints_the_rules(host: FakeHost):
    make_repo(host)
    block = (
        "<!-- house-style:generated:start -->\n"
        "1. Prefer pathlib (1 edit)\n"
        "<!-- house-style:generated:end -->\n"
    )
    (host.repo / "AGENTS.md").write_text(block, encoding="utf-8")
    assert "Prefer pathlib (1 edit)" in run(host, "export")


def test_unknown_subcommand_prints_usage(host: FakeHost):
    make_repo(host)
    assert "Usage: /house-style" in run(host, "nonsense")


def test_help_prints_usage(host: FakeHost):
    assert "Usage: /house-style" in run(host, "help")


def test_command_outside_a_repository_refuses(host: FakeHost, tmp_path: Path, monkeypatch):
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.setattr(host.plugin, "find_repo_root", lambda _path: None)
    assert "not inside a git repository" in run(host, "status")


def test_a_broken_data_directory_is_reported(host_factory, monkeypatch):
    host = host_factory()
    make_repo(host)
    monkeypatch.setattr(host.plugin, "_plugin_data_dir", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert "plugin data directory unavailable" in run(host, "status")


def test_commands_work_in_a_remote_session_directory(host_factory):
    """A Docker/SSH cwd does not exist on the host, so it becomes the logical root."""
    host = host_factory(remote={"/workspace/pkg/far.py": "value = 1\n"})
    make_repo(host)
    host.ctx.set_cwd(Path("/workspace"))
    out = run(host, "status")
    assert "House style — /workspace" in out, out
    assert "context file:" in out


def test_capture_uses_the_directory_the_session_reported(host: FakeHost):
    """A hook may run without the session's context; the session's own answer wins."""
    make_repo(host)
    host.ctx.set_cwd(host.repo)
    host.ctx.hooks["on_session_start"](session_id="s1")
    host.ctx.set_cwd(Path("/a/directory/only/the/hook/sees"))
    write_file(host, "pkg/module.py", "value = 1\n")
    signals = host.plugin.engine().scan(host.repo).signals
    assert [signal.path for signal in signals] == ["pkg/module.py"]
