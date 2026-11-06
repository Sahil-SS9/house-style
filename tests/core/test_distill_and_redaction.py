"""Batching, the distillation prompt, and redaction."""

from __future__ import annotations

from house_style_core import distill
from house_style_core.models import EDITED, KEPT, REVERTED, Verdict
from house_style_core.redaction import fallback_redact, make_redactor, redact_lines


def verdict(index: int, status: str = EDITED, path: str = "pkg/module.py") -> Verdict:
    if status == REVERTED:
        return Verdict(f"s{index}", path, status, before=[f"removed {index}"], after=[])
    if status == KEPT:
        return Verdict(f"s{index}", path, status)
    return Verdict(f"s{index}", path, status, before=[f"old {index}"], after=[f"new {index}"])


def test_select_pairs_prefers_edited_and_skips_kept():
    pairs = distill.select_pairs([verdict(1), verdict(2, KEPT), verdict(3, REVERTED)])
    assert [pair.status for pair in pairs] == [EDITED, REVERTED]


def test_select_pairs_is_bounded_by_count_and_characters():
    many = [verdict(index) for index in range(100)]
    pairs = distill.select_pairs(many, max_pairs=5, max_chars=10_000)
    assert len(pairs) == 5
    budgeted = distill.select_pairs(many, max_pairs=100, max_chars=120)
    assert sum(len(p.before) + len(p.after) + 40 for p in budgeted) <= 120


def test_select_pairs_deduplicates_identical_examples():
    same = [Verdict(f"s{index}", "pkg/module.py", EDITED, ["old"], ["new"]) for index in range(4)]
    assert len(distill.select_pairs(same)) == 1


def test_select_pairs_ignores_edited_verdicts_with_no_content():
    bare = [Verdict("s1", "pkg/module.py", EDITED, [], [])]
    assert distill.select_pairs(bare) == []


def test_build_messages_carries_pairs_and_existing_entries():
    pairs = [distill.Pair("pkg/module.py", ["old"], ["new"], EDITED)]
    messages = distill.build_messages(pairs, ["Prefer pathlib (2 edits)"])
    assert messages[0]["role"] == "system" and "house style" in messages[0]["content"]
    user = messages[1]["content"]
    assert "old" in user and "new" in user
    assert "Prefer pathlib (2 edits)" in user
    assert "pkg/module.py" in user


def test_diff_preview_shows_changed_lines():
    diff = distill.diff_preview("a\nb\n", "a\nc\n")
    assert "-b" in diff and "+c" in diff


def test_diff_preview_reports_no_change():
    assert distill.diff_preview("a\n", "a\n") == "(no change)"


def test_fallback_redaction_masks_common_token_shapes():
    text = (
        'key = "sk-abcdefghijklmnopqrstuvwx"\n'
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123\n"
        "aws = AKIAIOSFODNN7EXAMPLE\n"
        'password = "hunter2hunter2"\n'
        "url = postgres://user:secret@db.example/db\n"
    )
    out = fallback_redact(text)
    assert "sk-abcdefghijklmnop" not in out
    assert "ghp_abcdefghijkl" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "hunter2hunter2" not in out
    assert "secret@" not in out


def test_fallback_redaction_leaves_ordinary_code_alone():
    text = "MAX_TOKENS = 100\ndef add(a, b):\n    return a + b\n"
    assert fallback_redact(text) == text


def test_make_redactor_falls_back_when_the_host_redactor_raises():
    def broken(_text: str) -> str:
        raise RuntimeError("no redactor today")

    redact = make_redactor(broken)
    assert "sk-abcdefghijklmnopqrstuvwx" not in redact('key = "sk-abcdefghijklmnopqrstuvwx"')


def test_make_redactor_runs_both_layers():
    calls: list[str] = []

    def host(text: str) -> str:
        calls.append(text)
        return text.replace("INTERNAL-TOKEN", "[redacted]")

    redact = make_redactor(host)
    out = redact("a INTERNAL-TOKEN b sk-abcdefghijklmnopqrstuvwx")
    assert calls, "the host redactor must be consulted"
    assert "INTERNAL-TOKEN" not in out
    assert "sk-abcdefghijkl" not in out


def test_redact_lines_preserves_line_count_for_single_line_secrets():
    lines = ['token = "sk-abcdefghijklmnopqrstuvwx"', "other line"]
    redacted = redact_lines(lines, make_redactor())
    assert redacted is not None
    assert len(redacted) == 2
    assert "sk-abcdefghijkl" not in redacted[0]
    assert redacted[1] == "other line"
