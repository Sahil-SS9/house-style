"""Harness-agnostic helpers for talking to git.

Only two things are needed: find the repository a path belongs to, and read a
revision of a file. Both are optional — a directory that is not a git repository
yields ``None`` and the caller skips capture.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

MAX_UP = 48
GIT_TIMEOUT = 5.0


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


def is_inside(path: Path, root: Path) -> bool:
    """True when *path* sits at or under *root*, after resolving both."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def relative_to_root(root: Path, path: Path) -> str | None:
    """POSIX-style path relative to the repo root, or ``None`` when outside it."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return rel.as_posix()


def read_text_file(path: Path, *, max_bytes: int) -> str | None:
    """Read a regular text file, or ``None`` when missing, oversized, or unreadable."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def show_revision(root: Path, rel_path: str, revision: str = "HEAD") -> str | None:
    """``git show <revision>:<path>`` as text, or ``None`` when it is not there.

    Used only for diagnostics and for the ``committed`` note, never on the per-tool
    capture path.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:{rel_path}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def has_git(root: Path) -> bool:
    """True when ``root`` is a usable git repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def env_repo_root() -> Path | None:
    """Repo root for the current working directory, if there is one."""
    try:
        cwd = os.getcwd()
    except OSError:
        return None
    return find_repo_root(cwd)
