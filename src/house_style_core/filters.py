"""Size, binary and secret-file filters.

The rules are deliberately conservative: anything this module cannot vouch for is
skipped, and the caller is told why. The protected-path list itself belongs to the
harness, so it is injected as a predicate (``is_protected``) rather than duplicated
here — a hard-coded copy would drift from the host's own guard.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from pathlib import Path

#: A file bigger than this is never read back, so no signal is captured.
MAX_FILE_BYTES = 256 * 1024

#: Longest block of lines stored for one edit (both a whole-file write and a patch).
MAX_HUNK_LINES = 200

#: Longest block of characters stored for one edit.
MAX_HUNK_CHARS = 12_000

#: Context lines kept either side of the edit so it can be found again after shifts.
CONTEXT_LINES = 3

#: Files that routinely hold credentials. Matched on the basename only.
SECRET_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*",
    "secrets.*",
    "*_secret.*",
    "service-account*.json",
)

_BINARY_SNIFF_BYTES = 8192


def is_secret_like_name(path: str | Path) -> bool:
    """True for names that conventionally hold credentials."""
    name = Path(str(path)).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAME_PATTERNS)


def looks_binary(data: bytes) -> bool:
    """True when the first few KiB contain a NUL byte (a plain binary sniff)."""
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def skip_reason(
    path: Path,
    *,
    rel_path: str | None,
    size: int | None = None,
    text: str | None = None,
    is_protected: Callable[[str], bool] | None = None,
) -> str | None:
    """Why this file must not be captured, or ``None`` when it may be.

    ``is_protected`` is the harness's own protected-path predicate (in Hermes:
    ``agent.file_safety.is_write_denied``); pass ``None`` for a core-only run.
    """
    if rel_path is None:
        return "outside the repository"
    if is_secret_like_name(path):
        return "secret-like file name"
    if size is not None and size > MAX_FILE_BYTES:
        return f"file larger than {MAX_FILE_BYTES} bytes"
    try:
        if text is not None and looks_binary(text.encode("utf-8", "replace")):
            return "binary file"
        elif text is None and path.is_file() and looks_binary(path.read_bytes()[: _BINARY_SNIFF_BYTES + 1]):
            return "binary file"
    except OSError:
        return "unreadable file"
    if is_protected is not None:
        try:
            if is_protected(str(path)):
                return "protected path"
        except Exception:  # a broken host predicate must not lose the whole hook
            return "protected-path check failed"
    return None


def hunk_too_big(lines: list[str]) -> str | None:
    """Why this block of lines is too big to store, or ``None`` when it fits."""
    if len(lines) > MAX_HUNK_LINES:
        return f"block longer than {MAX_HUNK_LINES} lines"
    if sum(len(line) + 1 for line in lines) > MAX_HUNK_CHARS:
        return f"block larger than {MAX_HUNK_CHARS} characters"
    return None


def truncate_lines(lines: list[str], limit: int) -> list[str]:
    """First *limit* lines, unchanged (used for display, never for matching)."""
    return list(lines[:limit])


def redact_path_hint(rel_path: str) -> str:
    """Relative path with any home-like prefix stripped; never absolute."""
    return re.sub(r"^(\.\./)+", "", rel_path)
