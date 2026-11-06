"""Choosing the context file, and editing only our block inside it."""

from __future__ import annotations

from pathlib import Path

from house_style_core import contextfile as cf


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- choosing the file -------------------------------------------------------
def test_a_bare_repository_gets_a_new_agents_md(tmp_path: Path):
    target = cf.resolve_target(tmp_path)
    assert target.path == tmp_path / "AGENTS.md"
    assert target.kind == "AGENTS.md"
    assert target.exists is False


def test_an_existing_agents_md_is_used(tmp_path: Path):
    write(tmp_path / "AGENTS.md", "# AGENTS\n")
    target = cf.resolve_target(tmp_path)
    assert target.path == tmp_path / "AGENTS.md"
    assert target.exists is True


def test_hermes_md_wins_over_agents_md(tmp_path: Path):
    write(tmp_path / "AGENTS.md", "# AGENTS\n")
    write(tmp_path / "HERMES.md", "# hermes\n")
    assert cf.resolve_target(tmp_path).path == tmp_path / "HERMES.md"


def test_dot_hermes_md_wins_over_hermes_md(tmp_path: Path):
    write(tmp_path / "HERMES.md", "# hermes\n")
    write(tmp_path / ".hermes.md", "# dot\n")
    assert cf.resolve_target(tmp_path).path == tmp_path / ".hermes.md"


def test_agents_override_wins_within_a_directory(tmp_path: Path):
    write(tmp_path / "AGENTS.md", "# AGENTS\n")
    write(tmp_path / "AGENTS.override.md", "# override\n")
    assert cf.resolve_target(tmp_path).path == tmp_path / "AGENTS.override.md"


def test_claude_md_is_used_when_there_is_no_agents_file(tmp_path: Path):
    write(tmp_path / "CLAUDE.md", "# claude\n")
    assert cf.resolve_target(tmp_path).path == tmp_path / "CLAUDE.md"


def test_cursorrules_is_used_when_there_is_nothing_else(tmp_path: Path):
    write(tmp_path / ".cursorrules", "rules\n")
    assert cf.resolve_target(tmp_path).path == tmp_path / ".cursorrules"


def test_cursor_rule_files_are_used_when_there_is_no_cursorrules(tmp_path: Path):
    write(tmp_path / ".cursor" / "rules" / "style.mdc", "rules\n")
    target = cf.resolve_target(tmp_path)
    assert target.path.name == "style.mdc"
    assert target.kind == ".cursor/rules"


def test_a_hermes_md_above_the_working_directory_is_found(tmp_path: Path):
    write(tmp_path / ".hermes.md", "# root file\n")
    nested = tmp_path / "pkg" / "inner"
    nested.mkdir(parents=True)
    assert cf.resolve_target(tmp_path, nested).path == tmp_path / ".hermes.md"


def test_the_root_agents_md_is_preferred_over_a_nested_one(tmp_path: Path):
    write(tmp_path / "AGENTS.md", "# root\n")
    write(tmp_path / "pkg" / "AGENTS.md", "# nested\n")
    target = cf.resolve_target(tmp_path, tmp_path / "pkg")
    assert target.path == tmp_path / "AGENTS.md", "Hermes loads the git-root file first"


def test_a_working_directory_outside_the_repo_uses_the_repo_root(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert cf.resolve_target(repo, other).path == repo / "AGENTS.md"


# -- writing the block -------------------------------------------------------
def test_the_block_is_appended_and_the_rest_of_the_file_survives(tmp_path: Path):
    path = write(tmp_path / "AGENTS.md", "# AGENTS\n\nAlways run the tests.\n")
    rendered = cf.render(path.read_text(), ["Prefer pathlib (2 edits)"], [])
    assert "Always run the tests." in rendered
    assert cf.SECTION_TITLE in rendered
    assert "1. Prefer pathlib (2 edits)" in rendered
    assert cf.entries_from_text(rendered) == ["Prefer pathlib (2 edits)"]


def test_a_new_file_gets_a_header(tmp_path: Path):
    rendered = cf.render(None, ["Rule one (1 edit)"], [])
    assert rendered.startswith("# AGENTS.md")
    assert "1. Rule one (1 edit)" in rendered


def test_relearn_replaces_generated_lines_and_keeps_user_lines(tmp_path: Path):
    path = write(
        tmp_path / "AGENTS.md",
        "# AGENTS\n\n"
        + cf.START_MARKER
        + "\n1. Prefer pathlib (2 edits)\n- Keep en-GB spelling.\n"
        + cf.END_MARKER
        + "\n\nUnrelated trailing note.\n",
    )
    rendered = cf.render(
        path.read_text(),
        ["Prefer dataclasses for records (3 edits)"],
        ["Prefer pathlib (2 edits)"],
    )
    assert "Prefer dataclasses for records (3 edits)" in rendered
    assert "Prefer pathlib" not in rendered
    assert "Keep en-GB spelling." in rendered
    assert "Unrelated trailing note." in rendered


def test_a_user_edited_generated_line_survives(tmp_path: Path):
    path = write(
        tmp_path / "AGENTS.md",
        cf.START_MARKER + "\n1. Prefer pathlib over os.path (2 edits)\n" + cf.END_MARKER + "\n",
    )
    rendered = cf.render(
        path.read_text(),
        ["Prefer pathlib (5 edits)"],
        ["Prefer pathlib over os_path (2 edits)"],
    )
    assert "Prefer pathlib over os.path (2 edits)" in rendered
    assert "Prefer pathlib (5 edits)" in rendered


def test_rendering_twice_is_stable(tmp_path: Path):
    first = cf.render(None, ["Rule one (1 edit)"], [])
    second = cf.render(first, ["Rule one (1 edit)"], ["Rule one (1 edit)"])
    assert first == second


def test_an_empty_merge_removes_the_whole_section(tmp_path: Path):
    path = write(
        tmp_path / "AGENTS.md",
        "# AGENTS\n\nKeep me.\n\n## House style\n\n"
        + cf.START_MARKER
        + "\n1. Rule one (1 edit)\n"
        + cf.END_MARKER
        + "\n",
    )
    rendered = cf.render(path.read_text(), [], ["Rule one (1 edit)"])
    assert rendered.strip() == "# AGENTS\n\nKeep me."
    assert cf.SECTION_TITLE not in rendered


def test_an_empty_merge_keeps_a_line_the_user_added(tmp_path: Path):
    path = write(
        tmp_path / "AGENTS.md",
        cf.START_MARKER + "\n1. Rule one (1 edit)\n- My own line.\n" + cf.END_MARKER + "\n",
    )
    rendered = cf.render(path.read_text(), [], ["Rule one (1 edit)"])
    assert "My own line." in rendered
    assert "Rule one (1 edit)" not in rendered


def test_reading_entries_needs_no_block(tmp_path: Path):
    path = write(tmp_path / "AGENTS.md", "# AGENTS\n\nNothing learned yet.\n")
    assert cf.read_entries(path) == []


def test_cap_trims_on_a_line_boundary():
    text = "\n".join(f"line {index}" for index in range(50))
    capped, truncated = cf.cap(text, 40)
    assert truncated is True
    assert capped.endswith("...")
