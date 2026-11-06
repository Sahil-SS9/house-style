"""End-to-end behaviour of the core engine against real temporary git repositories."""

from __future__ import annotations

from pathlib import Path

from helpers import git, make_repo, write

from house_style_core.engine import PREFS_RELPATH, Engine
from house_style_core.models import EDITED, KEPT, REVERTED

MODULE = '''"""Small helper."""

import os


def config_path(root):
    return os.path.join(root, "config.json")
'''


def engine(tmp_path: Path, **kwargs) -> Engine:
    return Engine(tmp_path / "data", **kwargs)


def capture_write(eng: Engine, repo: Path, rel: str, text: str) -> None:
    path = write(repo / rel, text)
    result = eng.capture(repo_root=repo, file_path=path, tool="write_file", written_text=text)
    assert result.stored, result.reason


def test_capture_then_untouched_edit_reads_as_kept(tmp_path: Path):
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
    verdict = scan.verdicts[0]
    assert any("os.path.join" in line for line in verdict.before)
    assert any("pathlib.Path" in line for line in verdict.after)


def test_user_delete_reads_as_reverted(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    (repo / "pkg/helper.py").unlink()
    scan = eng.scan(repo)
    assert scan.counts[REVERTED] == 1


def test_patch_capture_records_the_replacement_block(tmp_path: Path):
    repo = make_repo(
        tmp_path / "repo",
        {"pkg/app.py": "import os\n\n\ndef path(root):\n    return os.path.join(root, 'a')\n"},
    )
    eng = engine(tmp_path)
    path = repo / "pkg/app.py"
    old = "    return os.path.join(root, 'a')"
    new = "    return root / 'a'"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    result = eng.capture(repo_root=repo, file_path=path, tool="patch", written_text=new, pre_text=old)
    assert result.stored
    assert result.signal is not None
    assert result.signal.hunk == [new]
    assert result.signal.pre_hunk == [old]
    assert result.signal.before_ctx, "a patch must record the line above it"
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
    eng.capture(repo_root=repo, file_path=path, tool="patch", written_text=new, pre_text=old)
    path.write_text(path.read_text().replace(new, old), encoding="utf-8")
    scan = eng.scan(repo)
    assert scan.counts[REVERTED] == 1
    assert "pre-edit" in scan.verdicts[0].note


def test_files_outside_the_repository_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    loose = write(tmp_path / "loose" / "notes.txt", "hello")
    result = eng.capture(repo_root=repo, file_path=loose, tool="write_file", written_text="hello")
    assert result.stored is False
    assert "outside" in result.reason


def test_secret_like_names_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    for name in (".env", "deploy.pem", "id_rsa", "credentials.json"):
        path = write(repo / name, "x = 1\nTOKEN=abcdef123456\n")
        result = eng.capture(repo_root=repo, file_path=path, tool="write_file", written_text=path.read_text())
        assert result.stored is False, name
        assert "secret-like" in result.reason


def test_oversized_files_are_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    big = "x" * (300 * 1024)
    path = write(repo / "big.py", big)
    result = eng.capture(repo_root=repo, file_path=path, tool="write_file", written_text=big)
    assert result.stored is False
    assert "larger" in result.reason


def test_the_harness_protected_list_is_honoured(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    protected = repo / "pkg" / "guarded.py"
    eng = engine(tmp_path, is_protected=lambda path: Path(path) == protected)
    path = write(protected, "value = 1\n")
    result = eng.capture(repo_root=repo, file_path=path, tool="write_file", written_text="value = 1\n")
    assert result.stored is False
    assert "protected" in result.reason


def test_a_block_too_long_to_store_is_skipped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    text = "\n".join(f"line {index}" for index in range(400)) + "\n"
    path = write(repo / "long.py", text)
    result = eng.capture(repo_root=repo, file_path=path, tool="write_file", written_text=text)
    assert result.stored is False
    assert "longer than" in result.reason


def test_secrets_are_redacted_before_they_are_stored(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    text = 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\nvalue = 1\n'
    capture_write(eng, repo, "pkg/settings.py", text)
    stored = eng.store.rows(eng.store.register_repo(repo))
    assert stored and "sk-abcdefghijkl" not in "\n".join(stored[0].hunk)


def test_redaction_does_not_make_an_untouched_secret_look_edited(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    text = 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\nvalue = 1\n'
    capture_write(eng, repo, "pkg/settings.py", text)
    assert eng.scan(repo).counts[KEPT] == 1


def test_injection_is_empty_until_preferences_exist(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    assert eng.injection(repo) is None
    write(repo / PREFS_RELPATH, "1. Rule one (1 edit)\n")
    block = eng.injection(repo)
    assert block is not None and "Rule one (1 edit)" in block


def test_injection_is_capped(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    entries = "\n".join(f"{index}. " + "long rule text " * 20 for index in range(1, 20))
    write(repo / PREFS_RELPATH, entries + "\n")
    block = eng.injection(repo)
    assert block is not None and len(block) < 2100


def test_stage_then_confirm_writes_the_file(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("os.path.join(root, ", "Path(root) / "))
    messages, pairs = eng.proposal(repo)
    assert pairs == 1 and messages
    eng.stage(repo, ["Prefer pathlib over os.path (1 edit)"])
    assert not (repo / PREFS_RELPATH).exists(), "staging must not write the file"
    eng.confirm(repo)
    text = (repo / PREFS_RELPATH).read_text(encoding="utf-8")
    assert "Prefer pathlib over os.path (1 edit)" in text


def test_confirm_keeps_a_user_edit_made_after_staging(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("os.path.join(root, ", "Path(root) / "))
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    write(
        repo / PREFS_RELPATH,
        "My own note.\n\n<!-- house-style:generated:start -->\n<!-- house-style:generated:end -->\n",
    )
    eng.confirm(repo)
    text = (repo / PREFS_RELPATH).read_text(encoding="utf-8")
    assert "My own note." in text
    assert "Prefer pathlib (1 edit)" in text


def test_confirm_without_a_proposal_is_a_no_op(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    assert eng.confirm(repo) is None


def test_reset_is_two_stage_and_keeps_user_lines(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    eng.confirm(repo)
    with open(repo / PREFS_RELPATH, "a", encoding="utf-8") as handle:
        handle.write("- A line I wrote myself.\n")

    planned = eng.reset(repo, confirmed=False)
    assert planned[0] == 1 and planned[2] == "planned"
    assert eng.store.rows(eng.store.register_repo(repo)), "planning must not delete anything"

    eng.reset(repo, confirmed=True)
    assert eng.store.rows(eng.store.register_repo(repo)) == []
    text = (repo / PREFS_RELPATH).read_text(encoding="utf-8")
    assert "A line I wrote myself." in text
    assert "Prefer pathlib (1 edit)" not in text


def test_off_flag_stops_capture(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    key = eng.store.register_repo(repo)
    eng.store.set_off(key, True)
    assert eng.store.is_off(key) is True


def test_status_reports_counts_and_the_newest_pairs(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    write(repo / "pkg/helper.py", MODULE.replace("return os.path.join", "return Path(root).joinpath"))
    text = eng.status(repo)
    assert "1 edited" in text
    assert "pkg/helper.py" in text
    assert "user " in text


def test_export_prints_the_file(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    assert "No .hermes/house-style.md" in eng.export(repo)
    write(repo / PREFS_RELPATH, "1. Rule (1 edit)\n")
    assert "1. Rule (1 edit)" in eng.export(repo)


def test_signals_are_keyed_by_repository(tmp_path: Path):
    first = make_repo(tmp_path / "one")
    second = make_repo(tmp_path / "two")
    eng = engine(tmp_path)
    capture_write(eng, first, "pkg/a.py", "a = 1\n")
    capture_write(eng, second, "pkg/b.py", "b = 2\n")
    assert len(eng.scan(first).signals) == 1
    assert len(eng.scan(second).signals) == 1
    assert eng.scan(first).signals[0].path == "pkg/a.py"


def test_a_committed_edit_still_classifies_against_the_worktree(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = engine(tmp_path)
    capture_write(eng, repo, "pkg/helper.py", MODULE)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "agent work")
    assert eng.scan(repo).counts[KEPT] == 1
