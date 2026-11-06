"""house-style core: learn a project's preferences from how you correct edits.

Harness-agnostic. Nothing here imports a host package; a harness adapter supplies

* the repository root and the file a tool wrote (``Engine.capture``),
* a redactor (``redaction.make_redactor``),
* a protected-path predicate (``filters`` callers pass ``is_protected``),
* and the model call for ``Engine.proposal``.

The Hermes plugin in ``house_style_hermes`` is one such adapter.
"""

from __future__ import annotations

from .distill import Pair, build_messages, diff_preview, select_pairs
from .engine import PREFS_RELPATH, Capture, Engine, Scan
from .filters import MAX_FILE_BYTES, MAX_HUNK_CHARS, MAX_HUNK_LINES, skip_reason
from .hunks import classify_signals, classify_text, find_anchored, find_exact, find_window, minimal_pair
from .models import EDITED, KEPT, REVERTED, STATUSES, Signal, Verdict, content_hash, utc_now
from .prefs import PrefsDoc, parse_response, read_prefs, render
from .redaction import Redactor, fallback_redact, make_redactor, redact_lines
from .store import Store, clear_all, repo_key

__all__ = [
    "EDITED",
    "Inject",
    "KEPT",
    "MAX_FILE_BYTES",
    "MAX_HUNK_CHARS",
    "MAX_HUNK_LINES",
    "PREFS_RELPATH",
    "Pair",
    "PrefsDoc",
    "REVERTED",
    "Redactor",
    "STATUSES",
    "Scan",
    "Signal",
    "Store",
    "Capture",
    "Engine",
    "Verdict",
    "build_messages",
    "classify_signals",
    "classify_text",
    "clear_all",
    "content_hash",
    "diff_preview",
    "fallback_redact",
    "find_anchored",
    "find_exact",
    "find_window",
    "make_redactor",
    "minimal_pair",
    "parse_response",
    "read_prefs",
    "redact_lines",
    "render",
    "repo_key",
    "select_pairs",
    "skip_reason",
    "utc_now",
]
