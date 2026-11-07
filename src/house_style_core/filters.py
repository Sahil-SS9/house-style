"""Size, binary, secret-name and location filters.

The rules are deliberately conservative: anything this module cannot vouch for is
skipped, and the caller is told why.

There is no protected-path predicate here by design. The host's own write guards run
before the tool call ever lands, so a blocked write produces no file change and
therefore no signal — capture is only ever recorded for writes the host permitted. A
public ``ctx.is_protected_path()`` would let a plugin pre-filter instead of relying on
that; it is requested in the lane's core seam note rather than reached for privately.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

#: A file bigger than this is never read back, so no signal is captured.
MAX_FILE_BYTES = 256 * 1024

#: Longest block of lines stored for one edit.
MAX_HUNK_LINES = 200

#: Longest block of characters stored for one edit.
MAX_HUNK_CHARS = 12_000

#: Context lines kept either side of the block so it can be found again after shifts.
CONTEXT_LINES = 3

#: Most occurrences captured for one ``replace_all`` edit.
MAX_OCCURRENCES = 50

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

#: Path segments never captured, whatever the file is called.
SKIP_SEGMENTS = (".git", ".hg", ".svn", "node_modules", "__pycache__")

_BINARY_SNIFF_BYTES = 8192


def is_secret_like_name(path: str | Path) -> bool:
    """True for names that conventionally hold credentials."""
    name = Path(str(path)).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAME_PATTERNS)


def has_skipped_segment(rel_path: str) -> bool:
    """True when the relative path runs through a directory we never look inside."""
    parts = rel_path.split("/")
    return any(part in SKIP_SEGMENTS for part in parts[:-1])


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
) -> str | None:
    """Why this file must not be captured, or ``None`` when it may be."""
    if rel_path is None:
        return "outside the repository"
    if has_skipped_segment(rel_path):
        return "inside a directory house-style never reads"
    if is_secret_like_name(path):
        return "secret-like file name"
    if size is not None and size > MAX_FILE_BYTES:
        return f"file larger than {MAX_FILE_BYTES} bytes"
    try:
        if text is not None:
            if looks_binary(text.encode("utf-8", "replace")):
                return "binary file"
        elif path.is_file() and looks_binary(path.read_bytes()[: _BINARY_SNIFF_BYTES + 1]):
            return "binary file"
    except OSError:
        return "unreadable file"
    return None


def hunk_too_big(lines: list[str]) -> str | None:
    """Why this block of lines is too big to store, or ``None`` when it fits."""
    if len(lines) > MAX_HUNK_LINES:
        return f"block longer than {MAX_HUNK_LINES} lines"
    if sum(len(line) + 1 for line in lines) > MAX_HUNK_CHARS:
        return f"block larger than {MAX_HUNK_CHARS} characters"
    return None
