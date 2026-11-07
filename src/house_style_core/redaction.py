"""Redaction seam.

The harness owns the real redactor: in Hermes that is ``ctx.redact_text()``. A plugin
must never invent its own idea of what a secret looks like and call it enough, so the
host redactor is passed in and applied to everything that gets stored.

A host redactor that *raises* is treated as a failure, not a fallback: the core raises
``RedactionError`` and the caller refuses to store or compare that text, because
storing unredacted text is worse than storing nothing.

The fallback below is a floor, not a substitute: it runs after the host redactor so a
host pass that misses a shape cannot put raw text on disk. It is intentionally narrow —
well-known token shapes, assignment to a credential-shaped name, private-key blocks.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: A redactor takes text and returns text with credentials replaced.
Redactor = Callable[[str], str]


class RedactionError(RuntimeError):
    """The host redactor failed. Callers must refuse to store or compare the text."""


_MASK = "[redacted]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Private key blocks, including the body.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        _MASK,
    ),
    # Well-known prefixed token shapes.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), _MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), _MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), _MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), _MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), _MASK),
    # Bearer-style headers.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{12,}"), r"\1 " + _MASK),
    # CREDENTIAL_NAME = "value" / CREDENTIAL_NAME: 'value'.
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_.\-]*(?:api[_-]?key|secret|token|passwd|password|passphrase|"
            r"private[_-]?key|access[_-]?key|client[_-]?secret)[A-Za-z0-9_.\-]*)"
            r"(\s*[:=]\s*)([\"']?)([^\s\"'#,;)]{6,})\3"
        ),
        r"\1\2\3" + _MASK + r"\3",
    ),
    # Credentials embedded in a URL.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + _MASK + "@"),
)


def fallback_redact(text: str) -> str:
    """Scrub well-known credential shapes from *text*. Never raises."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def make_redactor(primary: Redactor | None = None, *, strict: bool = False) -> Redactor:
    """Compose the harness redactor (when given) over the fallback.

    Both run, so a host redactor that misses a shape still gets the fallback pass.
    With ``strict`` a host redactor that raises propagates as :class:`RedactionError`
    so the caller refuses to store or compare; without it (stand-alone core use) the
    fallback alone is used.
    """

    def _redact(text: str) -> str:
        out = text
        if primary is not None:
            try:
                out = primary(out)
            except Exception as exc:
                if strict:
                    raise RedactionError(str(exc) or exc.__class__.__name__) from exc
                out = text
        return fallback_redact(out)

    return _redact


def redact_lines(lines: list[str] | None, redactor: Redactor) -> list[str] | None:
    """Redact a block of lines as one text, so multi-line patterns still match."""
    if lines is None:
        return None
    return redactor("\n".join(lines)).split("\n")
