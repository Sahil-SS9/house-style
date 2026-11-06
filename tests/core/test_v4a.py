"""Parsing multi-file V4A patches into per-file, per-hunk blocks."""

from __future__ import annotations

from house_style_core.v4a import parse_patch

MULTI = """*** Begin Patch
*** Update File: pkg/a.py
@@ def load(root): @@
 import os
-old = os.path.join(root, 'a')
+new = Path(root) / 'a'
 return new
*** Update File: pkg/a.py
-second hunk in the same file
+second hunk, rewritten
*** Update File: pkg/b.py
-one line in b
+rewritten line in b
*** Add File: pkg/c.py
+import pathlib
+
+def helper():
+    return pathlib.Path(".")
*** End Patch"""


def test_every_file_and_every_hunk_is_returned():
    parsed = parse_patch(MULTI)
    assert parsed.error is None
    paths = [hunk.path for hunk in parsed.hunks]
    assert paths == ["pkg/a.py", "pkg/a.py", "pkg/b.py", "pkg/c.py"]
    assert parsed.hunks[0].added == ["new = Path(root) / 'a'"]
    assert parsed.hunks[0].removed == ["old = os.path.join(root, 'a')"]
    assert parsed.hunks[0].context_before == ["import os"]
    assert parsed.hunks[0].context_after == ["return new"]
    assert parsed.hunks[1].added == ["second hunk, rewritten"]
    assert parsed.hunks[3].kind == "add"
    assert parsed.hunks[3].added == ["import pathlib", "", "def helper():", '    return pathlib.Path(".")']


def test_add_file_has_no_removed_side():
    parsed = parse_patch(MULTI)
    added = [hunk for hunk in parsed.hunks if hunk.kind == "add"][0]
    assert added.removed == []
    assert added.removed_text is None


def test_deletions_and_moves_are_reported_not_captured():
    parsed = parse_patch(
        "*** Begin Patch\n*** Delete File: gone.py\n*** Move File: a.py -> b.py\n*** End Patch"
    )
    assert parsed.hunks == []
    assert parsed.skipped == ["deleted gone.py", "moved a.py to b.py"]
    assert parsed.error is None


def test_an_empty_patch_is_not_an_error():
    assert parse_patch("").hunks == []
    assert parse_patch("").error is None


def test_crlf_line_endings_do_not_break_the_markers():
    parsed = parse_patch(MULTI.replace("\n", "\r\n"))
    assert parsed.error is None
    assert len(parsed.hunks) == 4


def test_a_patch_without_begin_and_end_markers_still_parses():
    parsed = parse_patch("*** Update File: pkg/a.py\n-old\n+new\n")
    assert parsed.hunks and parsed.hunks[0].added == ["new"]


def test_a_malformed_line_is_reported_and_keeps_what_came_before():
    parsed = parse_patch("*** Update File: pkg/a.py\n+good\nnot a patch line\n+bad\n")
    assert parsed.error is not None
    assert parsed.hunks and parsed.hunks[0].added == ["good"]


def test_blank_lines_inside_a_hunk_count_as_context():
    parsed = parse_patch("*** Update File: pkg/a.py\n above\n\n-below\n+below2\n")
    hunk = parsed.hunks[0]
    assert hunk.removed == ["below"]
    assert hunk.added == ["below2"]
    assert hunk.context_before == ["above", ""]
