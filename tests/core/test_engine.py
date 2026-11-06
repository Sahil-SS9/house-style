"""End-to-end behaviour of the core engine against real temporary git repositories."""

from __future__ import annotations

from pathlib import Path

from helpers import git, make_repo, write

from house_style_core import parse_patch
from house_style_core.engine import Block, Engine
from house_style_core.models import EDITED, KEPT, READ_TOOL, REVERTED

MODULE = '''"""Small helper."""

import os


def config_path(root):
    return os.path.join(root, "config.json")
'''


def engine(tmp_path: Path, **kwargs) -> Engine:
    kwargs.setdefault("redact", lambda text: text)
    return Engine(tmp_path / "data", **kwargs)


def capture_write(eng: Engine, repo: Path, rel: str, text: str) -> None:
    path = write(repo / rel, text)
    result = eng.capture(
        repo_root=repo,
        file_path=path,
        raw_path=rel,
        tool="write_file",
        blocks=[Block(text.splitlines(), raw=text)],
    )
    assert result.stored, result.reason


# -- capture and classification ---------------------------------------------
def test_capture_then_untouched_block_reads_as_kept(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    scan = eng.scan(repo)
    assert len(scan.signals) == 1
    assert scan.counts[KEPT] == 1


def test_user_rewrite_reads_as_edited_with_a_pair(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(
        repo / "pkg/helper.py",
        MODULE.replace('os.path.join(root, "config.json")', 'pathlib.Path(root) / "config.json"'),
    )
    scan = eng.scan(repo)
    assert scan.counts[EDITED] == 1
    assert any("os.path.join" in line for line in scan.verdicts[0].before)
    assert any("pathlib.Path" in line for line in scan.verdicts[0].after)


def test_user_delete_reads_as_reverted(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    (repo / "pkg/helper.py").unlink()
    assert eng.scan(repo).counts[REVERTED] == 1


def test_a_patch_records_the_replacement_block_and_its_fences(tmp_path: Path):
    repo = make_repo(
        tmp_path / "repo",
        {"pkg/app.py": "import os\n\n\ndef path(root):\n    return os.path.join(root, 'a')\n"},
    )
    eng = engine(tmp_path)
    path = repo / "pkg/app.py"
    old = "    return os.path.join(root, 'a')"
    new = "    return root / 'a'"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    result = eng.capture(
        repo_root=repo,
        file_path=path,
        raw_path="pkg/app.py",
        tool="patch",
        blocks=[Block(new.splitlines(), old.splitlines())],
    )
    assert result.stored
    signal = result.signals[0]
    assert signal.hunk == [new]
    assert signal.pre_hunk == [old]
    assert signal.before_ctx, "a patch must record the line above it"
    assert eng.scan(repo).counts[KEPT] == 1


def test_restoring_the_patched_line_reads_as_reverted(tmp_path: Path):
    repo = make_repo(
        tmp_path / "repo", {"pkg/app.py": "def path(root):\n    return os.path.join(root, 'a')\n"}
    )
    eng = engine(tmp_path)
    path = repo / "pkg/app.py"
    old = "    return os.path.join(root, 'a')"
    new = "    return root / 'a'"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    eng.capture(
        repo_root=repo,
        file_path=path,
        raw_path="pkg/app.py",
        tool="patch",
        blocks=[Block(new.splitlines(), old.splitlines())],
    )
    path.write_text(path.read_text().replace(new, old), encoding="utf-8")
    scan = eng.scan(repo)
    assert scan.counts[REVERTED] == 1
    assert "pre-edit" in scan.verdicts[0].note


def test_replace_all_captures_every_occurrence_and_judges_them_separately(tmp_path: Path):
    text = "def a():\n    return os.path.join(x, 'a')\n\ndef b():\n    return os.path.join(x, 'a')\n"
    repo = make_repo(tmp_path / "repo", {"pkg/two.py": text})
    eng = engine(tmp_path)
    old = "    return os.path.join(x, 'a')"
    new = "    return x / 'a'"
    path = repo / "pkg/two.py"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    result = eng.capture(
        repo_root=repo,
        file_path=path,
        raw_path="pkg/two.py",
        tool="patch",
        blocks=[Block(new.splitlines(), old.splitlines())],
    )
    assert result.stored
    assert [signal.occurrence for signal in result.signals] == [1, 2]
    assert all(signal.occurrences == 2 for signal in result.signals)
    assert result.signals[0].before_ctx != result.signals[1].before_ctx

    lines = path.read_text().splitlines()
    lines[4] = "    return pathlib.Path(x) / 'a'"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    counts = eng.scan(repo).counts
    assert counts[KEPT] == 1 and counts[EDITED] == 1, counts


def test_a_multi_file_v4a_patch_captures_every_file_and_hunk(tmp_path: Path):
    repo = make_repo(
        tmp_path / "repo",
        {"pkg/a.py": "def a():\n    return 1\n", "pkg/b.py": "def b():\n    return 2\n"},
    )
    eng = engine(tmp_path)
    write(repo / "pkg/a.py", "def a():\n    value = 1\n    return value\n")
    write(repo / "pkg/b.py", "def b():\n    return 3\n")
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: pkg/a.py\n"
        "-    return 1\n"
        "+    value = 1\n"
        "+    return value\n"
        "*** Update File: pkg/b.py\n"
        "-    return 2\n"
        "+    return 3\n"
        "*** End Patch"
    )
    grouped: dict[str, list[Block]] = {}
    for hunk in parse_patch(patch_text).hunks:
        grouped.setdefault(hunk.path, []).append(Block(hunk.added, hunk.removed or None))
    stored = []
    for rel, blocks in grouped.items():
        result = eng.capture(repo_root=repo, file_path=repo / rel, raw_path=rel, tool="patch", blocks=blocks)
        assert result.stored, result.reason
        stored.extend(result.signals)
    assert sorted({signal.path for signal in stored}) == ["pkg/a.py", "pkg/b.py"]
    assert len(stored) == 2


def test_a_write_that_does_not_land_is_not_captured(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    path = write(repo / "pkg/denied.py", "the file the host actually kept\n")
    result = eng.capture(
        repo_root=repo,
        file_path=path,
        raw_path="pkg/denied.py",
        tool="write_file",
        blocks=[Block(["what the agent asked to write"])],
    )
    assert result.stored is False
    assert "did not land" in result.reason


def test_files_outside_the_repository_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    loose = write(tmp_path / "loose" / "notes.txt", "hello")
    result = eng.capture(
        repo_root=repo,
        file_path=loose,
        raw_path=str(loose),
        tool="write_file",
        blocks=[Block(["hello"])],
    )
    assert result.stored is False
    assert "outside" in result.reason


def test_secret_like_names_and_git_internals_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    for name in (".env", "deploy.pem", "id_rsa", "credentials.json", ".git/config"):
        path = write(repo / name, "value = 1\n")
        result = eng.capture(
            repo_root=repo,
            file_path=path,
            raw_path=name,
            tool="write_file",
            blocks=[Block(["value = 1"])],
        )
        assert result.stored is False, name


def test_oversized_files_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    text = "x" * (300 * 1024)
    path = write(repo / "big.py", text)
    result = eng.capture(
        repo_root=repo, file_path=path, raw_path="big.py", tool="write_file", blocks=[Block([text])]
    )
    assert result.stored is False
    assert "larger" in result.reason


def test_capture_is_refused_without_a_host_redactor(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = Engine(tmp_path / "data", redact=None)
    result = eng.capture(
        repo_root=repo,
        file_path=write(repo / "pkg/x.py", "value = 1\n"),
        raw_path="pkg/x.py",
        tool="write_file",
        blocks=[Block(["value = 1"])],
    )
    assert eng.capture_enabled is False
    assert result.stored is False
    assert "no host redactor" in result.reason


# -- redaction ---------------------------------------------------------------
def test_the_host_redactor_runs_before_anything_is_stored(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    seen: list[str] = []

    def redact(text: str) -> str:
        seen.append(text)
        return text.replace("HOSTSECRET", "[redacted]")

    eng = engine(tmp_path, redact=redact)
    capture_write(eng, repo, "pkg/settings.py", "token = HOSTSECRET\nvalue = 1\n")
    assert seen, "the host redactor must be called"
    stored = eng.store.rows(eng.store.register_repo(repo))
    assert stored and "HOSTSECRET" not in "\n".join(stored[0].hunk)


def test_a_secret_the_user_did_not_touch_does_not_read_as_edited(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path, redact=lambda text: text.replace("TOKEN", "[redacted]"))
    capture_write(eng, repo, "pkg/settings.py", "key = TOKEN\nvalue = 1\n")
    assert eng.scan(repo).counts[KEPT] == 1


# -- remote backends ---------------------------------------------------------
def test_a_file_the_host_cannot_see_is_read_through_the_tool_surface(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    reads: list[tuple[str, str]] = []

    def read_remote(raw_path: str, task_id: str) -> str | None:
        reads.append((raw_path, task_id))
        return "value = 1\n"

    eng = engine(tmp_path, read_remote=read_remote)
    result = eng.capture(
        repo_root=repo,
        file_path=repo / "container-only" / "x.py",
        raw_path="/workspace/pkg/x.py",
        tool="write_file",
        blocks=[Block(["value = 1"])],
        task_id="docker-1",
    )
    assert result.stored, result.reason
    assert reads == [("/workspace/pkg/x.py", "docker-1")]
    assert result.signals[0].read_via == READ_TOOL


def test_an_unreadable_backend_is_recorded_and_named_in_status(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    result = eng.capture(
        repo_root=repo,
        file_path=repo / "container-only" / "x.py",
        raw_path="/workspace/pkg/x.py",
        tool="write_file",
        blocks=[Block(["value = 1"])],
        task_id="docker-7",
    )
    assert result.stored is False
    assert "could not read on backend docker-7" in eng.status(repo)


# -- the context file --------------------------------------------------------
def test_learn_writes_into_an_existing_agents_md_rather_than_a_new_file(tmp_path: Path):
    repo = make_repo(tmp_path / "repo", {"AGENTS.md": "# AGENTS\n\nRun the tests.\n"})
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("os.path.join(root, ", "Path(root) / "))
    _, after, target = eng.stage(repo, ["Prefer pathlib (1 edit)"])
    assert target == repo / "AGENTS.md"
    assert "Run the tests." in after
    assert not (repo / ".hermes").exists()
    eng.confirm(repo)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Prefer pathlib (1 edit)" in text and "Run the tests." in text


def test_learn_uses_dot_hermes_md_when_the_project_already_has_one(tmp_path: Path):
    repo = make_repo(tmp_path / "repo", {".hermes.md": "# House rules\n\nBe terse.\n"})
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("os.path.join(root, ", "Path(root) / "))
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    eng.confirm(repo)
    assert "Prefer pathlib (1 edit)" in (repo / ".hermes.md").read_text(encoding="utf-8")
    assert not (repo / "AGENTS.md").exists()


def test_stage_does_not_write_and_confirm_does(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    assert not (repo / "AGENTS.md").exists(), "staging must not write the file"
    eng.confirm(repo)
    assert (repo / "AGENTS.md").exists()


def test_confirm_keeps_a_user_edit_made_after_staging(tmp_path: Path):
    repo = make_repo(tmp_path / "repo", {"AGENTS.md": "# AGENTS\n"})
    eng = engine(tmp_path)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    write(repo / "AGENTS.md", "# AGENTS\n\nA note I added while deciding.\n")
    eng.confirm(repo)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "A note I added while deciding." in text and "Prefer pathlib (1 edit)" in text


def test_confirm_without_a_proposal_is_a_no_op(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    assert engine(tmp_path).confirm(repo) is None


def test_reset_is_two_stage_and_keeps_user_lines(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    eng.confirm(repo)
    with open(repo / "AGENTS.md", "a", encoding="utf-8") as handle:
        handle.write("- A line I wrote myself.\n")

    planned = eng.reset(repo, confirmed=False)
    assert planned[0] == 1 and planned[2] == "planned"
    assert eng.store.rows(eng.store.register_repo(repo)), "planning must not delete anything"

    eng.reset(repo, confirmed=True)
    assert eng.store.rows(eng.store.register_repo(repo)) == []
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "A line I wrote myself." in text
    assert "Prefer pathlib (1 edit)" not in text


def test_export_prints_the_rules(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    assert "No house-style rules" in eng.export(repo)
    eng.stage(repo, ["Rule (1 edit)"])
    eng.confirm(repo)
    assert "1. Rule (1 edit)" in eng.export(repo)


# -- reporting ---------------------------------------------------------------
def test_status_names_the_context_file_and_how_files_were_read(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("return os.path.join", "return Path(root).joinpath"))
    text = eng.status(repo)
    assert "context file: AGENTS.md" in text
    assert "capture: on" in text
    assert "host-disk 1" in text
    assert "1 edited" in text
    assert "pkg/helper.py" in text


def test_status_says_capture_is_off_without_a_redactor(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = Engine(tmp_path / "data", redact=None)
    text = eng.status(repo, capture_note="no ctx.redact_text() on this build")
    assert "capture: OFF — no ctx.redact_text() on this build" in text


def test_signals_are_keyed_by_repository(tmp_path: Path):
    first = make_repo(tmp_path / "one")
    second = make_repo(tmp_path / "two")
    eng = engine(tmp_path)
    capture_write(eng, first, "pkg/a.py", "a = 1\n")
    capture_write(eng, second, "pkg/b.py", "b = 2\n")
    assert [s.path for s in eng.scan(first).signals] == ["pkg/a.py"]
    assert [s.path for s in eng.scan(second).signals] == ["pkg/b.py"]


def test_a_committed_edit_still_classifies_against_the_worktree(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "agent work")
    assert eng.scan(repo).counts[KEPT] == 1


def test_scanning_and_reporting_create_no_files(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    eng.scan(repo)
    eng.status(repo)
    assert not (tmp_path / "data").exists(), "reading a project must not leave data behind"


def test_unreadable_files_are_reported_across_projects(tmp_path: Path):
    """A remote session may be filed under its launch directory; never drop it silently."""
    repo = make_repo(tmp_path / "repo")
    other = make_repo(tmp_path / "other")
    eng = engine(tmp_path)
    eng.capture(
        repo_root=other,
        file_path=other / "container-only" / "x.py",
        raw_path="/workspace/pkg/x.py",
        tool="write_file",
        blocks=[Block(["value = 1"])],
        task_id="docker-2",
    )
    text = eng.status(repo)
    assert "could not read on backend docker-2" in text
    assert "in other" in text
