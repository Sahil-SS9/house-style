"""Locating a captured edit again, and deciding what happened to it.

The problem this solves: between the moment the agent writes a block and the moment
we look at it again, unrelated edits can insert or remove lines above it, and the
user may have rewritten it. Line numbers are therefore useless on their own. A
match is found in three passes, cheapest first:

1. **exact** — the block still sits somewhere in the file, verbatim.
2. **anchored** — the lines immediately before and after the block are used as
   fences, so a single-line edit that was reworded can still be found and compared.
3. **window** — the most similar run of lines of the same length, scored with
   ``difflib``. Used only when both fences are gone.

A located block is then classified as kept (identical), reverted (matches the text
the block replaced, or vanished) or edited (anything else, recorded as a minimal
before/after pair).
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from .models import EDITED, KEPT, REVERTED, Signal, Verdict

#: Similarity at which a window match counts as untouched.
KEEP_RATIO = 0.995

#: Below this, a window match is treated as a rewrite rather than an edit.
EDIT_RATIO = 0.5

_MAX_ANCHOR_HITS = 200
_MAX_WINDOWS = 300
_MAX_REGION_LINES = 400
_ELLIPSIS = "..."


@dataclass(frozen=True)
class Match:
    """Where the captured block sits now, and how sure we are it is that block."""

    start: int
    lines: list[str]
    ratio: float
    how: str  # "exact" | "anchored" | "window"


def _norm(line: str) -> str:
    return line.rstrip()


def _same(a: Sequence[str], b: Sequence[str]) -> bool:
    if len(a) != len(b):
        return False
    return all(_norm(x) == _norm(y) for x, y in zip(a, b, strict=False))


def _occurrences(haystack: Sequence[str], needle: str, *, limit: int = _MAX_ANCHOR_HITS) -> list[int]:
    wanted = _norm(needle)
    out: list[int] = []
    for index, line in enumerate(haystack):
        if _norm(line) == wanted:
            out.append(index)
            if len(out) >= limit:
                break
    return out


def find_exact(haystack: Sequence[str], block: Sequence[str]) -> Match | None:
    """First verbatim occurrence of *block* in *haystack*."""
    if not block or len(block) > len(haystack):
        return None
    head = _norm(block[0])
    span = len(block)
    for index, line in enumerate(haystack[: len(haystack) - span + 1]):
        if _norm(line) != head:
            continue
        if _same(haystack[index : index + span], block):
            return Match(index, list(haystack[index : index + span]), 1.0, "exact")
    return None


def _fence(lines: Sequence[str], *, from_end: bool) -> tuple[str | None, int]:
    """Nearest non-blank fence line and how many lines sit between it and the block.

    Blank lines are useless as fences — they occur everywhere — so the search walks
    outwards past them and reports the gap, which the region calculation adds back.
    """
    ordered = list(reversed(lines)) if from_end else list(lines)
    for gap, line in enumerate(ordered):
        if line.strip():
            return line, gap
    return None, 0


def find_anchored(haystack: Sequence[str], signal: Signal) -> Match | None:
    """Locate the block using the lines that fenced it when it was written."""
    before_line, gap_before = _fence(signal.before_ctx, from_end=True)
    after_line, gap_after = _fence(signal.after_ctx, from_end=False)
    if before_line is None and after_line is None:
        return None
    want = len(signal.hunk)

    if before_line is not None and after_line is not None:
        opens = _occurrences(haystack, before_line)
        closes = _occurrences(haystack, after_line)
        if not opens or not closes:
            return None
        best: tuple[int, int, int] | None = None
        for start in opens:
            for close in closes:
                region_start = start + 1 + gap_before
                region_end = close - gap_after
                if region_end < region_start:
                    continue
                if region_end - region_start > _MAX_REGION_LINES:
                    continue
                # Prefer the fence pair whose gap is closest to the block we stored.
                score = abs((region_end - region_start) - want)
                if best is None or score < best[0]:
                    best = (score, region_start, region_end)
        if best is not None:
            _, region_start, region_end = best
            region = list(haystack[region_start:region_end])
            return Match(region_start, region, _ratio(signal.hunk, region), "anchored")
        return None

    if before_line is not None:
        for start in _occurrences(haystack, before_line):
            region_start = start + 1 + gap_before
            region = list(haystack[region_start : region_start + want + 20])
            if region:
                return Match(region_start, region, _ratio(signal.hunk, region), "anchored")
        return None

    for close in _occurrences(haystack, after_line or ""):
        region_end = close - gap_after
        region_start = max(0, region_end - want - 20)
        region = list(haystack[region_start:region_end])
        if region:
            return Match(region_start, region, _ratio(signal.hunk, region), "anchored")
    return None


def _ratio(block: Sequence[str], region: Sequence[str]) -> float:
    """Similarity of two blocks: line-aligned first, then character-level.

    ``SequenceMatcher`` over lists of lines only ever counts whole matching lines, so
    a single line the user reworded scores zero even when it is plainly the same
    statement. Falling back to a character-level score catches that case while still
    scoring a genuinely different block low.
    """
    if not block and not region:
        return 1.0
    if not block or not region:
        return 0.0
    if _same(block, region):
        return 1.0
    left = [_norm(x) for x in block]
    right = [_norm(x) for x in region]
    line_ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    if line_ratio >= EDIT_RATIO:
        return line_ratio
    char_ratio = difflib.SequenceMatcher(None, "\n".join(left), "\n".join(right), autojunk=False).ratio()
    return max(line_ratio, char_ratio)


def find_window(haystack: Sequence[str], block: Sequence[str]) -> Match | None:
    """Best-scoring run of *len(block)* lines, anchored on distinctive lines."""
    if not block or not haystack:
        return None
    span = len(block)
    anchors = sorted(
        {index for index, line in enumerate(block) if len(line.strip()) >= 4},
        key=lambda index: len(block[index].strip()),
        reverse=True,
    )[:3]
    if not anchors:
        anchors = [0]

    seen: set[int] = set()
    best: Match | None = None
    for anchor in anchors:
        for hit in _occurrences(haystack, block[anchor]):
            start = hit - anchor
            if start < 0 or start + span > len(haystack):
                continue
            if start in seen:
                continue
            seen.add(start)
            region = list(haystack[start : start + span])
            score = _ratio(block, region)
            if best is None or score > best.ratio:
                best = Match(start, region, score, "window")
            if score >= KEEP_RATIO:
                return best
            if len(seen) >= _MAX_WINDOWS:
                return best
    return best


def locate(haystack: Sequence[str], signal: Signal) -> Match | None:
    """Find the captured block in *haystack* by the cheapest pass that works."""
    if not haystack:
        return None
    match = find_exact(haystack, signal.hunk)
    if match is not None:
        return match
    match = find_anchored(haystack, signal)
    if match is not None:
        return match
    best = find_window(haystack, signal.hunk)
    if best is not None and best.ratio >= EDIT_RATIO:
        return best
    return None


def minimal_pair(
    block: Sequence[str],
    region: Sequence[str],
    *,
    max_lines: int = 24,
    context: int = 1,
) -> tuple[list[str], list[str]]:
    """Smallest before/after pair describing how *region* differs from *block*.

    Both sides are capped: the pair is a hint for distillation, not a diff to apply.
    """
    before_norm = [_norm(x) for x in block]
    after_norm = [_norm(x) for x in region]
    matcher = difflib.SequenceMatcher(None, before_norm, after_norm, autojunk=False)
    opcodes = matcher.get_opcodes()

    groups: list[tuple[int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if groups:
            pi1, pi2, pj1, pj2 = groups[-1]
            if i1 - pi2 <= context * 2:
                groups[-1] = (pi1, i2, pj1, j2)
                continue
        groups.append((i1, i2, j1, j2))
    if not groups:
        return [], []

    before: list[str] = []
    after: list[str] = []
    for i1, i2, j1, j2 in groups:
        before.extend(block[max(0, i1 - context) : i2 + context])
        after.extend(region[max(0, j1 - context) : j2 + context])
        before.append(_ELLIPSIS)
        after.append(_ELLIPSIS)

    def _cap(lines: list[str]) -> list[str]:
        if len(lines) <= max_lines:
            return lines
        return lines[: max_lines - 1] + [_ELLIPSIS]

    return _cap(before), _cap(after)


def classify_text(text: str | None, signal: Signal) -> Verdict:
    """Classify one captured edit against the file's current text."""
    base = Verdict(signal.id, signal.path, REVERTED)

    if not signal.hunk:
        base.note = "empty block"
        return base
    if text is None:
        base.before = list(signal.hunk)
        base.note = "file is gone"
        return base

    haystack = text.splitlines()
    match = locate(haystack, signal)
    if match is None:
        base.before = list(signal.hunk)
        base.note = "block no longer present"
        return base

    if match.how in ("exact", "anchored"):
        if _same(match.lines, signal.hunk):
            return Verdict(signal.id, signal.path, KEPT)
        if signal.pre_hunk is not None and _same(match.lines, signal.pre_hunk):
            return Verdict(signal.id, signal.path, REVERTED, note="restored to the pre-edit text")
        if not [line for line in match.lines if line.strip()]:
            return Verdict(signal.id, signal.path, REVERTED, note="block removed")
        # The fenced region may have grown or shrunk around the block (lines inserted
        # inside it, for example). Score the best span of the region before deciding.
        inner = find_window(match.lines, signal.hunk) if len(match.lines) != len(signal.hunk) else None
        if inner is not None and inner.ratio > match.ratio:
            if inner.ratio >= EDIT_RATIO:
                before, after = minimal_pair(signal.hunk, inner.lines)
                return Verdict(signal.id, signal.path, EDITED, before, after, "changed in place")
        if match.ratio >= EDIT_RATIO:
            before, after = minimal_pair(signal.hunk, match.lines)
            return Verdict(signal.id, signal.path, EDITED, before, after, "changed in place")
        return Verdict(signal.id, signal.path, REVERTED, note="rewritten beyond recognition")

    # Reached only when both fences and a verbatim match failed, so "kept" is not on
    # the table here: anything close enough to recognise counts as a rewrite.
    if match.ratio >= EDIT_RATIO:
        before, after = minimal_pair(signal.hunk, match.lines)
        return Verdict(signal.id, signal.path, EDITED, before, after, "changed in place")
    return Verdict(signal.id, signal.path, REVERTED, note="rewritten beyond recognition")


def classify_signals(
    signals: Sequence[Signal],
    read_text,
) -> list[Verdict]:
    """Classify many signals, reading each file at most once.

    *read_text* is ``fn(rel_path) -> str | None``; results are cached for the run so a
    file edited five times is read once.
    """
    cache: dict[str, str | None] = {}
    verdicts: list[Verdict] = []
    for signal in signals:
        if signal.path not in cache:
            cache[signal.path] = read_text(signal.path)
        verdicts.append(classify_text(cache[signal.path], signal))
    return verdicts
