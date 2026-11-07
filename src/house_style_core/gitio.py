"""Find the git repository a path belongs to, without running git.

A directory that is not inside a repository yields ``None`` and the caller skips capture.
"""

from __future__ import annotations

from pathlib import Path

MAX_UP = 48


def find_repo_root(path: str | Path, *, max_up: int = MAX_UP) -> Path | None:
    """Nearest ancestor of *path* holding a ``.git`` entry, or ``None``.

    A ``.git`` file counts: that is how linked worktrees and submodules look.
    No subprocess, so this is safe on a hot hook path.
    """
    try:
        start = Path(path).expanduser()
    except (TypeError, ValueError):
        return None
    if not start.is_absolute():
        return None
    current = start if start.is_dir() else start.parent
    for _ in range(max_up):
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def relative_to_root(root: Path, path: Path) -> str | None:
    """POSIX-style path relative to the repo root, or ``None`` when outside it."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return rel.as_posix()
