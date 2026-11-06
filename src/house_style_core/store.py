"""Bounded, per-repository JSONL store.

One file per repository keeps appends cheap: a tool call writes one line and, only
when the file grows past its cap, rewrites that single file. A repository is
identified by the SHA-256 of its resolved root path, so long paths do not become
file names and two checkouts never share rows.

Nothing is created until something is written: constructing a :class:`Store` touches
no disk. That matters because plugins are registered (and ``register()`` is run by
validators) long before a user edits anything.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import Signal

#: Rows kept per repository before the oldest are dropped.
MAX_ROWS_PER_REPO = 400

#: Bytes kept per repository before the oldest rows are dropped.
MAX_BYTES_PER_REPO = 512 * 1024

#: Repository files kept before the least recently written are removed.
MAX_REPOS = 50

#: A repository file untouched for this long is removed on the next sweep.
STALE_DAYS = 90

try:  # POSIX only; the fallback is a no-op lock on other platforms.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Exclusive advisory lock around one repository's files."""
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def repo_key(repo_root: str | Path) -> str:
    """Stable short key for a repository root."""
    import hashlib

    resolved = str(Path(repo_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:16]


class Store:
    """Signals, per-repository settings and learn proposals on disk."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base = Path(base_dir)

    # -- layout ---------------------------------------------------------
    @property
    def signals_dir(self) -> Path:
        return self.base / "signals"

    @property
    def pending_dir(self) -> Path:
        return self.base / "pending"

    def _rows_path(self, key: str) -> Path:
        return self.signals_dir / f"{key}.jsonl"

    def _meta_path(self, key: str) -> Path:
        return self.signals_dir / f"{key}.repo.json"

    def _lock_path(self, key: str) -> Path:
        return self.signals_dir / f"{key}.lock"

    def _pending_path(self, key: str) -> Path:
        return self.pending_dir / f"{key}.json"

    # -- repository metadata -------------------------------------------
    def set_meta(self, key: str, **values: Any) -> None:
        """Merge *values* into this repository's metadata file."""
        with _locked(self._lock_path(key)):
            meta = self._read_json(self._meta_path(key)) or {}
            meta.update(values)
            meta["updated"] = int(time.time())
            self._write_json(self._meta_path(key), meta)

    def meta(self, key: str) -> dict[str, Any]:
        return self._read_json(self._meta_path(key)) or {}

    def register_repo(self, repo_root: str | Path) -> str:
        """Record the repository root and return its key."""
        key = repo_key(repo_root)
        meta = self.meta(key)
        if meta.get("repo") != str(Path(repo_root).resolve()):
            self.set_meta(key, repo=str(Path(repo_root).resolve()))
        return key

    def is_off(self, key: str) -> bool:
        return bool(self.meta(key).get("off"))

    def set_off(self, key: str, off: bool) -> None:
        self.set_meta(key, off=bool(off))

    # -- signals --------------------------------------------------------
    def append(self, key: str, signal: Signal) -> None:
        """Append one signal and trim the file when it has outgrown its cap."""
        line = json.dumps(signal.to_dict(), ensure_ascii=False, separators=(",", ":"))
        path = self._rows_path(key)
        with _locked(self._lock_path(key)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._trim_locked(key)

    def rows(self, key: str) -> list[Signal]:
        """Every stored signal for one repository, oldest first."""
        path = self._rows_path(key)
        out: list[Signal] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(Signal.from_dict(json.loads(raw)))
                    except (ValueError, TypeError):
                        continue
        except OSError:
            return []
        return out

    def clear(self, key: str) -> int:
        """Delete every signal for one repository; returns how many rows went."""
        with _locked(self._lock_path(key)):
            rows = len(self._read_lines(self._rows_path(key)))
            try:
                self._rows_path(key).unlink()
            except OSError:
                pass
        return rows

    def _trim_locked(self, key: str) -> None:
        path = self._rows_path(key)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= MAX_BYTES_PER_REPO:
            lines = self._read_lines(path)
            if len(lines) <= MAX_ROWS_PER_REPO:
                return
        else:
            lines = self._read_lines(path)
        keep = lines[-MAX_ROWS_PER_REPO:]
        while keep and sum(len(line) + 1 for line in keep) > MAX_BYTES_PER_REPO:
            keep.pop(0)
        try:
            path.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
        except OSError:
            pass

    def _read_lines(self, path: Path) -> list[str]:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return [line for line in (raw.rstrip("\n") for raw in handle) if line.strip()]
        except OSError:
            return []

    # -- learn proposals -------------------------------------------------
    def set_pending(self, key: str, payload: dict[str, Any]) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self._pending_path(key), payload)

    def pending(self, key: str) -> dict[str, Any] | None:
        return self._read_json(self._pending_path(key))

    def clear_pending(self, key: str) -> None:
        try:
            self._pending_path(key).unlink()
        except OSError:
            pass

    # -- last generated entries -------------------------------------------
    def last_generated(self, key: str) -> list[str]:
        """Entries this plugin wrote last time (used to spot user edits)."""
        raw = self.meta(key).get("last_generated") or []
        return [str(item) for item in raw if str(item).strip()]

    def set_last_generated(self, key: str, entries: list[str]) -> None:
        self.set_meta(key, last_generated=[str(item) for item in entries])

    # -- housekeeping ----------------------------------------------------
    def tracked_repos(self) -> list[tuple[str, Path]]:
        """``(key, meta path)`` for every tracked repository."""
        out: list[tuple[str, Path]] = []
        try:
            for path in sorted(self.signals_dir.glob("*.repo.json")):
                out.append((path.name[: -len(".repo.json")], path))
        except OSError:
            return []
        return out

    def sweep(self, *, max_repos: int = MAX_REPOS, stale_days: int = STALE_DAYS) -> list[str]:
        """Drop stale and surplus repository files; returns the keys removed.

        Bounds the whole store without ever touching a live profile's other data:
        only files this plugin wrote, under its own plugin-data directory.
        """
        removed: list[str] = []
        cutoff = time.time() - stale_days * 86400
        entries: list[tuple[float, str]] = []
        for key, meta_path in self.tracked_repos():
            try:
                entries.append((meta_path.stat().st_mtime, key))
            except OSError:
                continue
        entries.sort(reverse=True)
        for index, (mtime, key) in enumerate(entries):
            stale = mtime < cutoff
            surplus = index >= max_repos
            if not (stale or surplus):
                continue
            for path in (
                self._rows_path(key),
                self._meta_path(key),
                self._pending_path(key),
                self._lock_path(key),
            ):
                try:
                    path.unlink()
                except OSError:
                    pass
            removed.append(key)
        return removed

    # -- json helpers ----------------------------------------------------
    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def clear_all(base_dir: str | Path) -> None:
    """Remove the plugin's whole data directory (used by ``reset`` at the top level)."""
    shutil.rmtree(Path(base_dir), ignore_errors=True)
