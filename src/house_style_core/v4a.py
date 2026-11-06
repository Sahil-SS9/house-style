"""A small reader for the V4A patch format the ``patch`` tool accepts.

Hermes grows the V4A dialect on some model families, but the handler accepts it from
any model, so a plugin that only looks at ``new_string`` silently misses every
multi-file edit. This module turns a V4A document into per-file, per-hunk blocks —
exactly the shape capture needs — without importing the host's own parser.

Format (matches ``tools/patch_parser.py`` at the pinned host)::

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     unchanged line
    -removed line
    +added line
    *** Add File: new/file.py
    +first line
    *** Delete File: gone.py
    *** Move File: old.py -> new.py
    *** End Patch

Deletions produce no block (there is no agent-written code left to judge) and moves
are reported as such, so the caller can say what it skipped rather than pretend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BEGIN = re.compile(r"^\*\*\*\s*Begin\s+Patch\s*$")
_END = re.compile(r"^\*\*\*\s*End\s+Patch\s*$")
_UPDATE = re.compile(r"^\*\*\*\s*Update\s+File:\s*(.+?)\s*$")
_ADD = re.compile(r"^\*\*\*\s*Add\s+File:\s*(.+?)\s*$")
_DELETE = re.compile(r"^\*\*\*\s*Delete\s+File:\s*(.+?)\s*$")
_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$")
_HINT = re.compile(r"^@@\s*(.*?)\s*@@\s*$")


@dataclass
class Hunk:
    """One changed region in one file."""

    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    kind: str = "update"  # "update" | "add"

    @property
    def added_text(self) -> str:
        return "\n".join(self.added)

    @property
    def removed_text(self) -> str | None:
        return "\n".join(self.removed) if self.removed else None


@dataclass
class ParsedPatch:
    hunks: list[Hunk] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None


def _flush(target: list[Hunk], hunk: Hunk | None) -> Hunk | None:
    if hunk is not None and (hunk.added or hunk.removed):
        # Trailing context belongs to the hunk that follows, not this one.
        target.append(hunk)
    return None


def parse_patch(text: str) -> ParsedPatch:
    """Parse a V4A document into per-file hunks.

    An empty patch is not an error. A malformed one returns ``error`` and whatever was
    parsed before the problem, so a caller can still capture the part that landed.
    """
    result = ParsedPatch()
    if not text or not text.strip():
        return result

    lines = [line.removesuffix("\r") for line in text.split("\n")]
    path: str | None = None
    kind = "update"
    hunk: Hunk | None = None

    for raw in lines:
        line = raw
        if _BEGIN.match(line) or _END.match(line):
            hunk = _flush(result.hunks, hunk)
            path = None
            continue
        if match := _UPDATE.match(line):
            hunk = _flush(result.hunks, hunk)
            path, kind = match.group(1).strip(), "update"
            continue
        if match := _ADD.match(line):
            hunk = _flush(result.hunks, hunk)
            path, kind = match.group(1).strip(), "add"
            continue
        if match := _DELETE.match(line):
            hunk = _flush(result.hunks, hunk)
            result.skipped.append(f"deleted {match.group(1).strip()}")
            path = None
            continue
        if match := _MOVE.match(line):
            hunk = _flush(result.hunks, hunk)
            result.skipped.append(f"moved {match.group(1).strip()} to {match.group(2).strip()}")
            path = None
            continue
        if _HINT.match(line):
            hunk = _flush(result.hunks, hunk)
            continue
        if path is None:
            continue
        if not line:
            # A blank line inside a hunk is a context line only when it carries the space
            # prefix; a bare blank ends nothing, so treat it as context.
            body, marker = "", " "
        else:
            marker, body = line[0], line[1:]
        if marker not in (" ", "+", "-"):
            result.error = f"unexpected line in a hunk: {line[:60]!r}"
            break
        if hunk is None:
            hunk = Hunk(path=path, kind=kind)
        if marker == "+":
            hunk.added.append(body)
        elif marker == "-":
            hunk.removed.append(body)
        else:
            if hunk.added or hunk.removed:
                hunk.context_after.append(body)
            else:
                hunk.context_before.append(body)
    _flush(result.hunks, hunk)
    return result
