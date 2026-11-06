"""Locating an edit again, and classifying it."""

from __future__ import annotations

from pathlib import Path

from house_style_core.hunks import classify_text, find_anchored, find_exact, find_window, minimal_pair
from house_style_core.models import EDITED, KEPT, REVERTED, Signal


def signal(
    hunk: list[str],
    before: list[str] | None = None,
    after: list[str] | None = None,
    pre: list[str] | None = None,
) -> Signal:
    return Signal(
        id="s1",
        ts="2026-01-01T00:00:00+00:00",
        tool="patch" if pre is not None else "write_file",
        path="pkg/module.py",
        content_sha256="0" * 64,
        hunk=hunk,
        before_ctx=before or [],
        after_ctx=after or [],
        pre_hunk=pre,
    )


def test_kept_when_block_is_untouched():
    sig = signal(["def add(a, b):", "    return a + b"], ["import os"], ["", "print(add(1, 2))"])
    text = "\n".join(["import os", "def add(a, b):", "    return a + b", "", "print(add(1, 2))"])
    verdict = classify_text(text, sig)
    assert verdict.status == KEPT
    assert verdict.before == [] and verdict.after == []


def test_kept_after_unrelated_lines_shift_above_it():
    sig = signal(["def add(a, b):", "    return a + b"], ["import os"], ["", "print(add(1, 2))"])
    text = "\n".join(
        ["import os", "# a comment", "# another", "", "def helper():", "    return 1"]
        + ["def add(a, b):", "    return a + b", "", "print(add(1, 2))"]
    )
    assert classify_text(text, sig).status == KEPT


def test_edited_inside_the_block_is_located_by_its_fences():
    sig = signal(
        ["def add(a, b):", "    return a + b"],
        ["import os"],
        ["", "print(add(1, 2))"],
    )
    text = "\n".join(
        [
            "import os",
            "# moved down",
            "",
            "def add(a, b):",
            "    total = a + b",
            "    return total",
            "",
            "print(add(1, 2))",
        ]
    )
    verdict = classify_text(text, sig)
    assert verdict.status == EDITED
    assert verdict.before, "an edited verdict must carry the agent's side"
    assert verdict.after, "an edited verdict must carry the user's side"
    assert "a + b" in "\n".join(verdict.before)
    assert "total" in "\n".join(verdict.after)


def test_single_line_patch_rewritten_is_edited_not_reverted():
    sig = signal(
        ['    path = os.path.join(root, "config.json")'],
        ["def load(root):"],
        ["    return path"],
        pre=["    path = os.path.join(root, 'config.json')"],
    )
    text = "\n".join(["def load(root):", "    path = root / 'config.json'", "    return path"])
    verdict = classify_text(text, sig)
    assert verdict.status == EDITED


def test_restored_to_the_pre_edit_text_is_reverted():
    pre = ["    return os.path.join(a, b)"]
    sig = signal(["    return pathlib.Path(a) / b"], ["def join(a, b):"], [""], pre=pre)
    text = "\n".join(["def join(a, b):", "    return os.path.join(a, b)", ""])
    verdict = classify_text(text, sig)
    assert verdict.status == REVERTED
    assert "pre-edit" in verdict.note


def test_block_removed_is_reverted():
    sig = signal(["    return a + b"], ["def add(a, b):"], ["", "print(1)"])
    text = "\n".join(["def add(a, b):", "    pass", "", "print(1)"])
    verdict = classify_text(text, sig)
    assert verdict.status == REVERTED


def test_missing_file_is_reverted_and_keeps_the_agent_side():
    sig = signal(["line one"], [], [])
    verdict = classify_text(None, sig)
    assert verdict.status == REVERTED
    assert verdict.before == ["line one"]
    assert "gone" in verdict.note


def test_no_fences_at_all_falls_back_to_a_similarity_window():
    sig = signal(["x = 1", "y = 2", "z = 3"])
    text = "\n".join(["a = 0", "x = 1", "y = 2", "z = 3", "b = 4"])
    assert classify_text(text, sig).status == KEPT


def test_rewritten_beyond_recognition_is_reverted():
    sig = signal(["x = 1", "y = 2", "z = 3"])
    text = "\n".join(["totally different", "content here", "nothing alike"])
    assert classify_text(text, sig).status == REVERTED


def test_trailing_whitespace_does_not_count_as_a_user_edit():
    sig = signal(["value = compute()"], ["setup = 1"], ["print(value)"])
    text = "\n".join(["setup = 1", "value = compute()   ", "print(value)"])
    assert classify_text(text, sig).status == KEPT


def test_minimal_pair_is_bounded():
    block = [f"line {index}" for index in range(60)]
    region = [f"line {index}" if index % 2 else f"changed {index}" for index in range(60)]
    before, after = minimal_pair(block, region, max_lines=10)
    assert len(before) <= 10 and len(after) <= 10
    assert before and after


def test_find_exact_and_anchored_agree_on_position(tmp_path: Path):
    sig = signal(["b", "c"], ["a"], ["d"])
    lines = ["a", "b", "c", "d"]
    exact = find_exact(lines, ["b", "c"])
    assert exact is not None
    assert (exact.start, exact.how) == (1, "exact")
    shifted = ["a", "x", "y", "b", "c", "d"]
    anchored = find_anchored(shifted, sig)
    assert anchored is not None and anchored.how == "anchored"
    assert "b" in anchored.lines and "c" in anchored.lines
    inner = find_window(anchored.lines, ["b", "c"])
    assert inner is not None and inner.start == 2


def test_empty_block_is_not_classified_as_kept():
    sig = signal([])
    assert classify_text("anything", sig).status == REVERTED
