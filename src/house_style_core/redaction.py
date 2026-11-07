"""Redaction seam: the harness owns the redactor (in Hermes, ``ctx.redact_text()``).

The core never decides on its own what a secret looks like. A host redactor that raises is a
failure, not something to work around: :class:`RedactionError` makes the caller refuse to store or
compare that text, because storing unredacted text is worse than storing nothing.
"""

from __future__ import annotations

from collections.abc import Callable

#: A redactor takes text and returns text with credentials replaced.
Redactor = Callable[[str], str]


class RedactionError(RuntimeError):
    """The host redactor failed. Callers must refuse to store or compare the text."""


def make_redactor(primary: Redactor) -> Redactor:
    """Wrap the host redactor so any failure, or a non-string result, is a RedactionError."""

    def _redact(text: str) -> str:
        try:
            out = primary(text)
        except Exception as exc:
            raise RedactionError(str(exc) or type(exc).__name__) from exc
        if not isinstance(out, str):
            raise RedactionError(f"the redactor returned {type(out).__name__}, not str")
        return out

    return _redact


def redact_lines(lines: list[str] | None, redactor: Redactor) -> list[str] | None:
    """Redact a block of lines as one text, so multi-line patterns still match."""
    return None if lines is None else redactor("\n".join(lines)).split("\n")
