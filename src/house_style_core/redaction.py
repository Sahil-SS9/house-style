"""Redaction seam.

The harness owns the real redactor: in Hermes that is
``agent.redact.redact_sensitive_text``. A plugin must never invent its own idea of
what a secret looks like and call it enough, so the harness redactor is passed in
and applied with ``force=True`` — it must hold regardless of the user's
``security.redact_secrets`` preference.

The fallback below exists so the core package is usable stand-alone (tests, another
harness) and so a host whose redactor fails to import still stores something
scrubbed rather than raw file content. It is intentionally narrow: well-known token
shapes, assignment to a credential-shaped name, and private-key blocks.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: A redactor takes text and returns text with credentials replaced.
Redactor = Callable[[str], str]

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


def make_redactor(primary: Redactor | None = None) -> Redactor:
    """Compose the harness redactor (when given) over the fallback.

    Both run: a host redactor that misses a shape still gets the fallback pass, and
    a host redactor that raises degrades to the fallback instead of storing raw text.
    """

    def _redact(text: str) -> str:
        out = text
        if primary is not None:
            try:
                out = primary(out)
            except Exception:
                out = text
        return fallback_redact(out)

    return _redact


def redact_lines(lines: list[str] | None, redactor: Redactor) -> list[str] | None:
    """Redact a block of lines as one text, so multi-line patterns still match."""
    if lines is None:
        return None
    return redactor("\n".join(lines)).split("\n")
