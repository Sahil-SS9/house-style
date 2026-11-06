"""The preferences file: parsing, rendering, and the merge that keeps user lines."""

from __future__ import annotations

from pathlib import Path

from house_style_core import prefs


def test_parse_response_accepts_numbered_and_bulleted_lists():
    reply = (
        "Here is the merged list:\n\n"
        "1. Prefer pathlib over os.path (4 edits)\n"
        "2) Return early instead of nesting (2 edits)\n"
        "- Use en-GB spelling in user-facing strings (2 edits)\n"
        "* Keep imports at the top (1 edit)\n"
    )
    assert prefs.parse_response(reply) == [
        "Prefer pathlib over os.path (4 edits)",
        "Return early instead of nesting (2 edits)",
        "Use en-GB spelling in user-facing strings (2 edits)",
        "Keep imports at the top (1 edit)",
    ]


def test_parse_response_drops_headings_fences_and_prose():
    reply = "## Merged preferences\n```\n1. Prefer dataclasses for records (3 edits)\n```\nThat is all.\n"
    entries = prefs.parse_response(reply)
    assert entries == ["Prefer dataclasses for records (3 edits)"]


def test_parse_response_deduplicates_case_insensitively_and_caps():
    reply = "\n".join([f"{i}. Same rule (1 edit)" for i in range(1, 6)])
    assert prefs.parse_response(reply) == ["Same rule (1 edit)"]

    many = "\n".join(f"{i}. Rule number {i} (1 edit)" for i in range(1, 30))
    assert len(prefs.parse_response(many)) == prefs.MAX_ENTRIES


def test_render_creates_the_file_with_a_generated_block(tmp_path: Path):
    doc = prefs.read_prefs(tmp_path / "missing.md")
    text = prefs.render(doc, ["Prefer pathlib (2 edits)"], [])
    assert prefs.START_MARKER in text and prefs.END_MARKER in text
    assert "1. Prefer pathlib (2 edits)" in text
    assert prefs.DEFAULT_HEADER.splitlines()[0] in text


def test_relearn_replaces_generated_lines_and_keeps_user_lines(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(
        "# House style\n\nMy own note outside the block.\n\n"
        + prefs.START_MARKER
        + "\n1. Prefer pathlib (2 edits)\n- Use tabs, not spaces.\n"
        + prefs.END_MARKER
        + "\n",
        encoding="utf-8",
    )
    doc = prefs.read_prefs(path)
    text = prefs.render(doc, ["Prefer dataclasses for records (3 edits)"], ["Prefer pathlib (2 edits)"])
    assert "Prefer dataclasses for records (3 edits)" in text
    assert "Prefer pathlib" not in text, "the previous generated line is replaced"
    assert "Use tabs, not spaces." in text, "a line the user wrote survives re-learning"
    assert "My own note outside the block." in text


def test_user_edited_version_of_a_generated_line_survives(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(
        prefs.START_MARKER + "\n1. Prefer pathlib over os.path (2 edits)\n" + prefs.END_MARKER + "\n",
        encoding="utf-8",
    )
    doc = prefs.read_prefs(path)
    # The user tightened the wording; the stored "last generated" line no longer matches.
    text = prefs.render(doc, ["Prefer pathlib (5 edits)"], ["Prefer pathlib over os_path (2 edits)"])
    assert "Prefer pathlib over os.path (2 edits)" in text
    assert "Prefer pathlib (5 edits)" in text


def test_merge_into_a_file_without_a_block_appends_one(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text("# Notes\n\nSomething I wrote.\n", encoding="utf-8")
    doc = prefs.read_prefs(path)
    text = prefs.render(doc, ["Rule one (1 edit)"], [])
    assert text.startswith("# Notes\n")
    assert "Something I wrote." in text
    assert prefs.START_MARKER in text and "Rule one (1 edit)" in text


def test_rendering_twice_is_stable(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(prefs.START_MARKER + "\n" + prefs.END_MARKER + "\n", encoding="utf-8")
    first = prefs.render(prefs.read_prefs(path), ["Rule one (1 edit)"], [])
    path.write_text(first, encoding="utf-8")
    second = prefs.render(prefs.read_prefs(path), ["Rule one (1 edit)"], ["Rule one (1 edit)"])
    assert first == second


def test_entries_are_read_back_without_numbering(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(
        prefs.START_MARKER + "\n1. First rule (2 edits)\n2. Second rule (1 edit)\n" + prefs.END_MARKER,
        encoding="utf-8",
    )
    assert prefs.read_prefs(path).entries() == ["First rule (2 edits)", "Second rule (1 edit)"]


def test_strip_block_empties_only_the_generated_region(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(
        "keep me\n" + prefs.START_MARKER + "\n1. Rule\n" + prefs.END_MARKER + "\nafter\n",
        encoding="utf-8",
    )
    stripped = prefs.strip_block(path.read_text(encoding="utf-8"))
    assert "1. Rule" not in stripped
    assert "keep me" in stripped and "after" in stripped


def test_cap_trims_on_a_line_boundary():
    text = "\n".join(f"line {index}" for index in range(50))
    capped, truncated = prefs.cap(text, 40)
    assert truncated is True
    assert capped.endswith("...")
    assert len(capped) <= 44


def test_cap_leaves_short_text_alone():
    text = "one line"
    assert prefs.cap(text, 40) == (text, False)


def test_describe_counts_entries(tmp_path: Path):
    path = tmp_path / "house-style.md"
    path.write_text(prefs.START_MARKER + "\n1. A (1 edit)\n" + prefs.END_MARKER, encoding="utf-8")
    assert prefs.describe(prefs.read_prefs(path)) == "1 entry"
