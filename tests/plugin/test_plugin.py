"""The adapter: registration, hook wiring and the command surface."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import FakeHost, build_host

from house_style_core.engine import PREFS_RELPATH


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
        result="ok",
        status=status,
        task_id="default",
        session_id="s1",
        turn_id="t1",
    )


def patch_file(host: FakeHost, rel: str, old: str, new: str) -> None:
    path = host.repo / rel
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    host.ctx.hooks["post_tool_call"](
        tool_name="patch",
        args={"path": rel, "old_string": old, "new_string": new},
        result="ok",
        status="ok",
        task_id="default",
        session_id="s1",
        turn_id="t1",
    )


def run(host: FakeHost, args: str = "") -> str:
    return host.ctx.commands["house-style"]["handler"](args)


# -- registration -----------------------------------------------------------
def test_register_declares_the_three_hooks_and_one_command(host: FakeHost):
    assert set(host.ctx.hooks) == {"post_tool_call", "on_session_start", "pre_llm_call"}
    assert set(host.ctx.commands) == {"house-style"}
    assert host.ctx.tools == {}, "house-style must not expose a model tool"


def test_registration_writes_nothing(host: FakeHost):
    assert not host.data_dir.exists(), "register() must not create the data directory"


def test_registration_skips_a_hook_the_host_does_not_define(tmp_path, monkeypatch):
    built, _ = build_host(tmp_path, monkeypatch)
    built.plugin._hook_supported = lambda name: name != "on_session_start"
    built.ctx.hooks.clear()
    built.plugin.register(built.ctx)
    assert "on_session_start" not in built.ctx.hooks
    assert "post_tool_call" in built.ctx.hooks


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


def test_the_preferences_file_itself_is_not_captured(host: FakeHost):
    make_repo(host)
    write_file(host, PREFS_RELPATH, "1. something\n")
    assert "signals: 0" in run(host, "status")


def test_protected_paths_are_not_captured(host: FakeHost):
    make_repo(host)
    write_file(host, "blocked.py", "value = 1\n")
    assert "signals: 0" in run(host, "status")


def test_patch_is_captured_using_the_replacement_text(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/app.py", "def run():\n    return 1\n")
    patch_file(host, "pkg/app.py", "    return 1", "    return 2")
    status = run(host, "status")
    assert "signals: 2" in status


def test_off_stops_capture_and_on_resumes_it(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    assert "house-style is off for project" in run(host, "off")
    write_file(host, "pkg/other.py", "value = 2\n")
    assert "signals: 1" in run(host, "status")
    run(host, "on")
    write_file(host, "pkg/third.py", "value = 3\n")
    assert "signals: 2" in run(host, "status")


# -- classification ---------------------------------------------------------
def test_session_start_scans_and_an_edit_is_visible(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "def run():\n    return 1\n")
    (host.repo / "pkg/module.py").write_text("def run():\n    return 42\n", encoding="utf-8")
    host.ctx.hooks["on_session_start"](session_id="s1", model="m", platform="cli")
    status = run(host, "status")
    assert "1 edited" in status
    assert "pkg/module.py" in status


def test_scan_command_reports_counts(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    out = run(host, "scan")
    assert "Scanned 1 recorded edit" in out
    assert "1 kept" in out


# -- injection --------------------------------------------------------------
def test_injection_happens_on_the_first_turn_only(host: FakeHost):
    make_repo(host)
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (2 edits)\n", encoding="utf-8")
    first = host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=True)
    assert first and "Prefer pathlib (2 edits)" in first["context"]
    second = host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=False)
    assert second is None


def test_injection_off_setting_prevents_it(host: FakeHost):
    make_repo(host)
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (2 edits)\n", encoding="utf-8")
    host.ctx.settings["inject"] = "off"
    assert host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=True) is None


def test_unknown_injection_setting_refuses_and_is_reported(host: FakeHost):
    make_repo(host)
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (2 edits)\n", encoding="utf-8")
    host.ctx.settings["inject"] = "sometimes"
    assert host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=True) is None
    assert "unknown inject setting" in run(host, "status")


def test_injection_is_off_for_a_repository_that_was_turned_off(host: FakeHost):
    make_repo(host)
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (2 edits)\n", encoding="utf-8")
    run(host, "off")
    assert host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=True) is None


def test_injection_is_skipped_outside_a_repository(host: FakeHost, monkeypatch):
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (2 edits)\n", encoding="utf-8")
    monkeypatch.setattr(host.plugin, "find_repo_root", lambda _path: None)
    assert host.ctx.hooks["pre_llm_call"](session_id="s1", is_first_turn=True) is None


# -- learn ------------------------------------------------------------------
def test_learn_stages_a_proposal_and_confirm_writes_it(host_factory):
    host, set_cwd = host_factory(reply="1. Prefer pathlib over os.path (1 edit)\n")
    set_cwd(make_repo(host).repo)
    write_file(host, "pkg/module.py", "import os\n\nprint(os.path.join('a', 'b'))\n")
    (host.repo / "pkg/module.py").write_text(
        "from pathlib import Path\n\nprint(Path('a') / 'b')\n", encoding="utf-8"
    )
    run(host, "scan")
    out = run(host, "learn")
    assert "Staged 1 preference" in out
    assert "Prefer pathlib over os.path (1 edit)" in out
    assert host.ctx.llm.calls, "the model must be called for learn"
    messages = host.ctx.llm.calls[0]["messages"]
    assert "value = 1" not in messages[1]["content"], "only the changed lines are sent"
    assert not (host.repo / PREFS_RELPATH).exists(), "learn alone must not write the file"
    confirm = run(host, "learn confirm")
    assert "Wrote .hermes/house-style.md" in confirm
    assert "Prefer pathlib over os.path (1 edit)" in (host.repo / PREFS_RELPATH).read_text(encoding="utf-8")


def test_learn_with_nothing_to_learn_from_does_not_call_the_model(host_factory):
    host, set_cwd = host_factory(reply="1. Rule (1 edit)\n")
    set_cwd(make_repo(host).repo)
    out = run(host, "learn")
    assert "Nothing to learn from yet" in out
    assert host.ctx.llm.calls == []


def test_confirm_without_a_proposal_says_so(host: FakeHost):
    make_repo(host)
    assert "No proposal is waiting" in run(host, "learn confirm")


def test_learn_reports_an_unusable_model_reply(host_factory):
    host, set_cwd = host_factory(reply="I could not find a pattern.")
    set_cwd(make_repo(host).repo)
    write_file(host, "pkg/module.py", "value = 1\n")
    (host.repo / "pkg/module.py").write_text("value = 2\n", encoding="utf-8")
    run(host, "scan")
    assert "no usable preferences" in run(host, "learn")


# -- reset, export, usage ---------------------------------------------------
def test_reset_is_two_stage(host: FakeHost):
    make_repo(host)
    write_file(host, "pkg/module.py", "value = 1\n")
    planned = run(host, "reset")
    assert "Would clear 1 stored edit" in planned
    assert "signals: 1" in run(host, "status")
    assert "Cleared 1 stored edit" in run(host, "reset confirm")
    assert "signals: 0" in run(host, "status")


def test_export_prints_the_preferences_file(host: FakeHost):
    make_repo(host)
    (host.repo / PREFS_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (host.repo / PREFS_RELPATH).write_text("1. Prefer pathlib (1 edit)\n", encoding="utf-8")
    assert "Prefer pathlib (1 edit)" in run(host, "export")


def test_unknown_subcommand_prints_usage(host: FakeHost):
    make_repo(host)
    assert "Usage: /house-style" in run(host, "nonsense")


def test_help_prints_usage(host: FakeHost):
    assert "Usage: /house-style" in run(host, "help")


def test_command_outside_a_repository_refuses(host: FakeHost, monkeypatch):
    monkeypatch.setattr(host.plugin, "_current_repo", lambda: None)
    assert "not inside a git repository" in run(host, "status")


def test_status_and_learn_survive_a_broken_data_directory(host_factory):
    host, set_cwd = host_factory()
    set_cwd(make_repo(host).repo)

    def boom() -> Path:
        raise RuntimeError("no data dir for you")

    host.plugin._plugin_data_dir = boom
    assert "plugin data directory unavailable" in run(host, "status")
