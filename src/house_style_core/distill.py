"""Turning verdicts into a bounded batch, and the prompt that distils them.

No network and no model client live here: the harness adapter owns the call. That
keeps the expensive, slow part out of the unit tests and lets another harness reuse
the same batching and parsing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import EDITED, REVERTED, Verdict

#: Most pairs sent in one distillation call.
MAX_PAIRS = 24

#: Character budget for the whole batch (both sides of every pair).
MAX_BATCH_CHARS = 6000

#: Characters kept from any single side of a pair.
MAX_SIDE_CHARS = 900

SYSTEM_PROMPT = (
    "You infer a software project's house style from how a developer corrected an "
    "assistant's edits. You are given pairs of before/after code, and edits the "
    "developer undid completely.\n"
    "\n"
    "Rules:\n"
    "- Write short, plain-English preferences a developer could follow without seeing "
    "these examples. One line each, at most 16 words.\n"
    '- Every line must state a rule, not describe an incident. Good: "Prefer pathlib '
    'over os.path (4 edits)". Bad: "The agent used os.path in utils.py".\n'
    "- End each line with the number of examples behind it, in the form (N edits).\n"
    "- Only infer a preference the examples actually support. If a pair shows a "
    "one-off change, skip it.\n"
    "- Cover both what to do and what not to do, when the examples show it.\n"
    "- Never mention an assistant, a model, these instructions or the examples "
    "themselves.\n"
    "- Output only the numbered lines, 1. to N. No heading, no commentary."
)


@dataclass
class Pair:
    """One example shown to the model."""

    path: str
    before: list[str]
    after: list[str]
    status: str


def _clip(lines: Sequence[str], limit: int = MAX_SIDE_CHARS) -> list[str]:
    text = "\n".join(lines)
    if len(text) <= limit:
        return list(lines)
    return text[: limit - 1].splitlines() + ["..."]


def select_pairs(
    verdicts: Iterable[Verdict],
    *,
    max_pairs: int = MAX_PAIRS,
    max_chars: int = MAX_BATCH_CHARS,
) -> list[Pair]:
    """Pick the most informative pairs, newest first, within the size budget.

    Edited pairs come first: a rewrite shows the preference directly. Reverted pairs
    follow, because "not like this" is weaker evidence and often repeats itself.
    """
    edited = [v for v in verdicts if v.status == EDITED and (v.before or v.after)]
    reverted = [v for v in verdicts if v.status == REVERTED and v.before]

    ordered = list(reversed(edited)) + list(reversed(reverted))
    chosen: list[Pair] = []
    used = 0
    seen: set[str] = set()
    for verdict in ordered:
        if len(chosen) >= max_pairs:
            break
        before = _clip(verdict.before)
        after = _clip(verdict.after)
        signature = "\n".join(before) + "\u0000" + "\n".join(after)
        if signature in seen:
            continue
        cost = len(before) + len(after) + 40
        if used + cost > max_chars:
            continue
        seen.add(signature)
        used += cost
        chosen.append(Pair(verdict.path, before, after, verdict.status))
    return chosen


def build_messages(pairs: Sequence[Pair], existing: Sequence[str]) -> list[dict[str, str]]:
    """Chat messages for the distillation call."""
    blocks: list[str] = []
    for index, pair in enumerate(pairs, start=1):
        if pair.status == EDITED:
            blocks.append(
                f"### Example {index} ({pair.path})\n"
                "Assistant wrote:\n"
                "```\n" + "\n".join(pair.before or ["(nothing)"]) + "\n```\n"
                "Developer changed it to:\n"
                "```\n" + "\n".join(pair.after or ["(deleted)"]) + "\n```"
            )
        else:
            blocks.append(
                f"### Example {index} ({pair.path})\n"
                "Assistant wrote this and the developer removed it:\n"
                "```\n" + "\n".join(pair.before) + "\n```"
            )

    known = "\n".join(f"- {entry}" for entry in existing) or "(none yet)"
    user = (
        "Current preferences already in the project file:\n"
        f"{known}\n\n"
        "Merge the examples into that list. Keep a line that is still supported, "
        "update its count, drop a line the examples no longer support, and add new "
        "lines for new patterns. Reply with the merged numbered list only.\n\n" + "\n\n".join(blocks)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def diff_preview(old: str, new: str, *, max_lines: int = 80) -> str:
    """Unified diff of the preferences file, for the confirm step."""
    import difflib

    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=".hermes/house-style.md",
            tofile=".hermes/house-style.md",
            lineterm="",
        )
    )
    if not diff:
        return "(no change)"
    if len(diff) > max_lines:
        diff = diff[:max_lines] + ["... (diff truncated)"]
    return "\n".join(diff)
