"""Review of 2920179 (NousResearch/hermes-agent#150474).

1. The ``@@ hint @@`` check runs on every line of every patch inside a turn-blocking hook, and
   the model writes the patch. A long ``@@`` line with no closing ``@@`` made the old regex
   backtrack for seconds (1.7 s at 1.6 KB, 26.7 s at 3.2 KB). It must stay linear.
2. ``learn confirm`` and ``reset confirm`` must never write through a symlinked context file:
   a symlinked ``AGENTS.md`` could point anywhere.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from helpers import make_repo

from house_style_core import parse_patch, v4a
from house_style_core.engine import Engine

OLD_HINT = re.compile(r"^@@\s*(.*?)\s*@@\s*$")  # the 0.3.0 form, kept here as the reference


def _patch(line: str) -> str:
    return f"*** Begin Patch\n*** Update File: a.py\n{line}\n-old\n+new\n*** End Patch\n"


@pytest.mark.parametrize("size", [4000, 64_000])
def test_a_long_unclosed_hint_line_parses_in_linear_time(size: int):
    line = "@@" + " " * size + "x"
    start = time.perf_counter()
    result = parse_patch(_patch(line))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.25, f"{size}-char hint line took {elapsed:.2f}s"
    # Not a hint (no closing @@), so it is reported as a bad line, exactly as before.
    assert result.error and "unexpected line" in result.error


def test_the_hint_check_agrees_with_the_old_pattern_on_ordinary_lines():
    samples = [
        "@@ def load(root): @@",
        "@@def x@@",
        "@@ @@",
        "@@@@",
        "@@ class A: @@   ",
        "@@",
        "@@ no closing",
        " @@ leading space @@",
        "@@ tab\t@@\t",
        "text @@ x @@",
        "@@@",
        "",
    ]
    for line in samples:
        assert v4a.is_hint(line) == bool(OLD_HINT.match(line)), repr(line)


def _engine(tmp_path: Path) -> Engine:
    return Engine(tmp_path / "data", redact=lambda text: text)


def test_confirm_refuses_a_symlinked_context_file(tmp_path: Path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not yours\n", encoding="utf-8")
    repo = make_repo(tmp_path / "repo")
    (repo / "AGENTS.md").symlink_to(outside)
    eng = _engine(tmp_path)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    with pytest.raises(ValueError, match="symlink"):
        eng.confirm(repo)
    assert outside.read_text(encoding="utf-8") == "not yours\n"


def test_confirm_refuses_a_context_file_swapped_for_a_symlink_after_staging(tmp_path: Path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not yours\n", encoding="utf-8")
    repo = make_repo(tmp_path / "repo", {"AGENTS.md": "# AGENTS\n"})
    eng = _engine(tmp_path)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        eng.confirm(repo)
    assert outside.read_text(encoding="utf-8") == "not yours\n"


def test_reset_refuses_a_symlinked_context_file(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    eng = _engine(tmp_path)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    eng.confirm(repo)
    outside = tmp_path / "elsewhere.md"
    outside.write_text((repo / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside)
    before = outside.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        eng.reset(repo, confirmed=True)
    assert outside.read_text(encoding="utf-8") == before


def test_a_regular_context_file_is_still_written(tmp_path: Path):
    repo = make_repo(tmp_path / "repo", {"AGENTS.md": "# AGENTS\n"})
    eng = _engine(tmp_path)
    eng.stage(repo, ["Prefer pathlib (1 edit)"])
    eng.confirm(repo)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Prefer pathlib (1 edit)" in text and not (repo / "AGENTS.md").is_symlink()
