"""The preferences file: format, parsing and the merge that never loses user text.

The file is plain markdown the user owns. Learned entries live between two HTML
comments so a re-learn can replace exactly its own region and nothing else. Lines
inside that region which the user added or rewrote are carried across verbatim: the
merge compares the current region against what was generated last time and treats
every difference as the user's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

START_MARKER = "<!-- house-style:generated:start -->"
END_MARKER = "<!-- house-style:generated:end -->"

MAX_ENTRIES = 12
MAX_ENTRY_CHARS = 200

DEFAULT_HEADER = (
    "# House style\n"
    "\n"
    "Preferences learned from how you corrected the agent's edits in this project.\n"
    "You own this file: edit it, delete lines, or delete the file. Re-learning merges\n"
    "and never drops a line you wrote. `/house-style reset` clears the learned lines.\n"
    "\n"
    "## Learned preferences\n"
)

_NUMBERING = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*```")


@dataclass
class PrefsDoc:
    """A preferences file, split into the parts the merge needs."""

    exists: bool
    text: str
    inside_block: list[str]

    @property
    def has_block(self) -> bool:
        return START_MARKER in self.text and END_MARKER in self.text

    def entries(self) -> list[str]:
        """Entries inside the generated region, with numbering and bullets removed.

        A hand-written file with no markers is read as a plain list, so a user can write
        ``.hermes/house-style.md`` by hand and still get it injected.
        """
        raw = self.inside_block
        if not raw and self.exists and not self.has_block:
            raw = [
                line for line in self.text.splitlines() if line.strip() and not line.lstrip().startswith("#")
            ]
        return [line for line in (clean_entry(item) for item in raw) if line]


def read_prefs(path: Path) -> PrefsDoc:
    """Read the preferences file; a missing file is an empty document, not an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return PrefsDoc(False, "", [])
    return PrefsDoc(True, text, _block_lines(text))


def _block_lines(text: str) -> list[str]:
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end < 0 or end <= start:
        return []
    body = text[start + len(START_MARKER) : end]
    return [line for line in body.splitlines() if line.strip()]


def clean_entry(raw: str) -> str:
    """Strip list numbering, bullets and surrounding whitespace from one entry."""
    line = _NUMBERING.sub("", raw.strip())
    line = line.strip()
    if not line or line.startswith("<!--"):
        return ""
    if len(line) > MAX_ENTRY_CHARS:
        line = line[: MAX_ENTRY_CHARS - 1].rstrip() + "\u2026"
    return line


def normalise(entries: Iterable[str]) -> list[str]:
    """Deduplicate entries, case-insensitively, keeping the first spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        clean = clean_entry(entry)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def parse_response(text: str) -> list[str]:
    """Pull preference entries out of a model reply.

    Only a numbered or bulleted list is accepted: a stray sentence must never become a
    permanent line in the user's file, so prose is dropped rather than guessed at. Code
    fences, headings, HTML comments and a lead-in line ending in a colon
    (``Here is the merged list:``) are ignored. The output is capped, because a runaway
    reply must not land in the user's file.
    """
    listed: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _FENCE.match(line) or line.startswith("#") or line.startswith("<!--"):
            continue
        if _NUMBERING.match(line):
            listed.append(line)
    return normalise(listed)[:MAX_ENTRIES]


def render(doc: PrefsDoc, learned: Sequence[str], last_generated: Sequence[str]) -> str:
    """Merge *learned* into *doc*, preserving every line the user owns."""
    prior = {clean_entry(line).casefold() for line in last_generated if clean_entry(line)}
    preserved: list[str] = []
    for raw in doc.inside_block:
        clean = clean_entry(raw)
        if not clean:
            continue
        if clean.casefold() in prior:
            continue  # a line we generated last time; the new list replaces it
        preserved.append(raw.strip())

    merged = normalise(list(learned) + preserved)[: MAX_ENTRIES + len(preserved)]
    numbered = [f"{index}. {entry}" for index, entry in enumerate(merged[:MAX_ENTRIES], start=1)]
    extras = merged[MAX_ENTRIES:]

    body = list(numbered)
    if extras:
        body.append("")
        body.extend(extras)
    block = START_MARKER + "\n" + "\n".join(body) + "\n" + END_MARKER

    if not doc.exists:
        return DEFAULT_HEADER + "\n" + block + "\n"
    if doc.has_block:
        start = doc.text.index(START_MARKER)
        end = doc.text.index(END_MARKER) + len(END_MARKER)
        head, tail = doc.text[:start], doc.text[end:]
        return head + block + (tail if tail else "\n")
    separator = "" if doc.text.endswith("\n\n") else ("\n" if doc.text.endswith("\n") else "\n\n")
    return doc.text + separator + "## Learned preferences\n\n" + block + "\n"


def strip_block(text: str) -> str:
    """The file with the generated region emptied (used by ``reset``)."""
    if START_MARKER not in text or END_MARKER not in text:
        return text
    start = text.index(START_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    return text[:start] + START_MARKER + "\n" + END_MARKER + text[end:]


def cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Trim *text* to *max_chars* on a line boundary; returns ``(text, truncated)``."""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    return cut.rstrip() + "\n...", True


def describe(doc: PrefsDoc) -> str | None:
    """One-line summary for status output, or ``None`` when there are no entries."""
    entries = doc.entries()
    if not entries:
        return None
    return f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
