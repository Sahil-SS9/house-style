"""house-style core: learn a project's house style from how you correct edits.

Harness-agnostic. Nothing here imports a host package; a harness adapter supplies

* the repository root and the raw path of the file a tool wrote (``Engine.capture``),
* how to read a file the host cannot see, for a remote backend (``read_remote``),
* the host's redactor, or ``None`` when the host has none (capture is then refused),
* and the model call for ``Engine.proposal``.

The Hermes plugin in ``house_style_hermes`` is one such adapter.
"""

from __future__ import annotations

from . import contextfile, v4a
from .contextfile import Target, clean_entry, read_entries, render, resolve_target
from .distill import Pair, build_messages, diff_preview, parse_response, select_pairs
from .engine import Block, Capture, Engine, Scan
from .filters import (
    MAX_FILE_BYTES,
    MAX_HUNK_CHARS,
    MAX_HUNK_LINES,
    MAX_OCCURRENCES,
    skip_reason,
)
from .hunks import (
    classify_signals,
    classify_text,
    find_all_exact,
    find_anchored,
    find_exact,
    find_window,
    minimal_pair,
)
from .models import (
    EDITED,
    KEPT,
    READ_HOST,
    READ_TOOL,
    REVERTED,
    STATUSES,
    Signal,
    Verdict,
    content_hash,
    utc_now,
)
from .redaction import Redactor, fallback_redact, make_redactor, redact_lines
from .store import Store, clear_all, repo_key
from .v4a import Hunk, ParsedPatch, parse_patch

__all__ = [
    "Block",
    "Capture",
    "EDITED",
    "Engine",
    "Hunk",
    "KEPT",
    "MAX_FILE_BYTES",
    "MAX_HUNK_CHARS",
    "MAX_HUNK_LINES",
    "MAX_OCCURRENCES",
    "Pair",
    "ParsedPatch",
    "READ_HOST",
    "READ_TOOL",
    "REVERTED",
    "Redactor",
    "STATUSES",
    "Scan",
    "Signal",
    "Store",
    "Target",
    "Verdict",
    "build_messages",
    "classify_signals",
    "classify_text",
    "clean_entry",
    "clear_all",
    "content_hash",
    "contextfile",
    "diff_preview",
    "fallback_redact",
    "find_all_exact",
    "find_anchored",
    "find_exact",
    "find_window",
    "make_redactor",
    "minimal_pair",
    "parse_patch",
    "parse_response",
    "read_entries",
    "redact_lines",
    "render",
    "repo_key",
    "resolve_target",
    "select_pairs",
    "skip_reason",
    "utc_now",
    "v4a",
]
