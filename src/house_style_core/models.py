"""Record types for house-style.

Two families live here:

* :class:`Signal` — one block of lines the agent introduced, captured straight after
  the tool call, together with the lines either side of it so it can be found again
  after unrelated edits move it.
* :class:`Verdict` — what the user did with that block: kept, edited or reverted.

Every stored string is already redacted by the caller (see ``redaction``); nothing in
this module re-interprets content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 2

KEPT = "kept"
EDITED = "edited"
REVERTED = "reverted"
STATUSES = (KEPT, EDITED, REVERTED)

#: How a captured file's text was obtained: read on the host, or through the tool
#: surface because the file lives on a remote backend.
READ_HOST = "host-disk"
READ_TOOL = "tool-surface"


def utc_now() -> str:
    """Second-resolution UTC timestamp, ``2026-11-06T13:04:05+00:00``."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    """SHA-256 of the exact text the agent wrote (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def new_signal_id() -> str:
    """Short opaque id: a hash of time and object identity, no content or path in it."""
    return hashlib.sha256(f"{utc_now()}:{id(object())}".encode()).hexdigest()[:16]


@dataclass
class Signal:
    """One captured block."""

    id: str
    ts: str
    tool: str
    path: str
    content_sha256: str
    hunk: list[str]
    before_ctx: list[str] = field(default_factory=list)
    after_ctx: list[str] = field(default_factory=list)
    pre_hunk: list[str] | None = None
    start_line: int = 0
    total_lines: int = 0
    size: int = 0
    read_via: str = READ_HOST
    occurrence: int = 1
    occurrences: int = 1
    session_id: str = ""
    turn_id: str = ""
    task_id: str = ""
    v: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "ts": self.ts,
            "tool": self.tool,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "hunk": list(self.hunk),
            "before_ctx": list(self.before_ctx),
            "after_ctx": list(self.after_ctx),
            "pre_hunk": None if self.pre_hunk is None else list(self.pre_hunk),
            "start_line": self.start_line,
            "total_lines": self.total_lines,
            "size": self.size,
            "read_via": self.read_via,
            "occurrence": self.occurrence,
            "occurrences": self.occurrences,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        """Rebuild a signal, tolerating rows written by an older schema."""
        pre = data.get("pre_hunk")
        return cls(
            id=str(data.get("id", "")),
            ts=str(data.get("ts", "")),
            tool=str(data.get("tool", "")),
            path=str(data.get("path", "")),
            content_sha256=str(data.get("content_sha256", "")),
            hunk=[str(x) for x in (data.get("hunk") or [])],
            before_ctx=[str(x) for x in (data.get("before_ctx") or [])],
            after_ctx=[str(x) for x in (data.get("after_ctx") or [])],
            pre_hunk=None if pre is None else [str(x) for x in pre],
            start_line=int(data.get("start_line") or 0),
            total_lines=int(data.get("total_lines") or 0),
            size=int(data.get("size") or 0),
            read_via=str(data.get("read_via") or READ_HOST),
            occurrence=int(data.get("occurrence") or 1),
            occurrences=int(data.get("occurrences") or 1),
            session_id=str(data.get("session_id", "")),
            turn_id=str(data.get("turn_id", "")),
            task_id=str(data.get("task_id", "")),
            v=int(data.get("v") or 1),
        )


@dataclass
class Verdict:
    """What happened to one captured block."""

    signal_id: str
    path: str
    status: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "path": self.path,
            "status": self.status,
            "before": list(self.before),
            "after": list(self.after),
            "note": self.note,
        }
