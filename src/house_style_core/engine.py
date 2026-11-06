"""The harness-agnostic engine: capture, classify, report, distil.

Everything that touches a repository, a context file or the store lives here. A
harness adapter only has to say *where* the repo is, *what* the tool wrote, *how* to
read a file the host cannot see, *who* redacts, and *who* calls the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import contextfile, distill
from .filters import (
    CONTEXT_LINES,
    MAX_FILE_BYTES,
    MAX_OCCURRENCES,
    count_lines,
    hunk_too_big,
    skip_reason,
)
from .gitio import find_repo_root, relative_to_root
from .hunks import classify_signals, find_all_exact
from .models import (
    KEPT,
    READ_HOST,
    READ_TOOL,
    Signal,
    Verdict,
    content_hash,
    new_signal_id,
    utc_now,
)
from .redaction import Redactor, make_redactor, redact_lines
from .store import Store, repo_key

#: Newest edited pairs shown by ``status``.
STATUS_PAIRS = 10

#: Most times one tool call can produce a block set before we stop counting.
MAX_BLOCKS = 400


@dataclass
class Block:
    """One block of lines the agent introduced, and what it replaced."""

    added: list[str]
    pre: list[str] | None = None
    #: The exact text the tool was asked to write, when known. Used to confirm the
    #: write landed: a denied or failed write leaves the file unchanged, and nothing
    #: should be recorded for it.
    raw: str | None = None


@dataclass
class Capture:
    """Outcome of one tool call's capture attempt."""

    stored: bool
    reason: str = ""
    signals: list[Signal] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.signals)


@dataclass
class Scan:
    """Outcome of one classification pass."""

    repo: Path
    key: str
    signals: list[Signal]
    verdicts: list[Verdict]

    @property
    def counts(self) -> dict[str, int]:
        out = {KEPT: 0, "edited": 0, "reverted": 0}
        for verdict in self.verdicts:
            out[verdict.status] = out.get(verdict.status, 0) + 1
        return out


class Engine:
    """Capture and learn, without knowing which harness called."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        redact: Redactor | None = None,
        read_remote: Callable[[str, str], str | None] | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.store = Store(base_dir)
        self.redact = redact
        self.redactor = make_redactor(redact)
        self.read_remote = read_remote
        self.max_file_bytes = max_file_bytes
        self._read_cache: dict[str, tuple[float, str | None]] = {}

    @property
    def capture_enabled(self) -> bool:
        """Capture needs a host redactor; without one nothing is stored at all."""
        return self.redact is not None

    # -- reading ----------------------------------------------------------
    def _read_remote(self, raw_path: str, task_id: str, file_path: Path) -> str | None:
        if self.read_remote is None:
            return None
        try:
            return self.read_remote(raw_path, task_id, str(file_path))
        except TypeError:
            return self.read_remote(raw_path, task_id)  # a two-argument reader
        except Exception:
            return None

    def _read_back(self, file_path: Path, raw_path: str, task_id: str) -> tuple[str | None, str]:
        """Text of the file as the tool left it, and how we got it."""
        try:
            return file_path.read_text(encoding="utf-8", errors="replace"), READ_HOST
        except OSError:
            pass
        text = self._read_remote(raw_path, task_id, file_path)
        if text is not None:
            return text, READ_TOOL
        return None, ""

    # -- capture ---------------------------------------------------------
    def capture(
        self,
        *,
        repo_root: str | Path,
        file_path: str | Path,
        raw_path: str,
        tool: str,
        blocks: list[Block],
        rel_path: str | None = None,
        task_id: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> Capture:
        """Record the blocks *tool* introduced at *file_path*.

        The file is read back (host disk, else the tool surface) so the stored hash and
        context describe what is actually there. A block that cannot be found in the
        file never landed, so it is skipped rather than guessed at.
        """
        if not self.capture_enabled:
            return Capture(False, "no host redactor available, so nothing is recorded")
        try:
            root = Path(repo_root).resolve()
            path = Path(file_path)
        except OSError:
            return Capture(False, "path unavailable")

        rel_path = rel_path or relative_to_root(root, path)
        if rel_path is None:
            logical = str(path).replace("\\", "/")
            base = str(root).replace("\\", "/").rstrip("/") + "/"
            rel_path = logical[len(base) :] if logical.startswith(base) else None
        if rel_path is not None:
            rel_path = rel_path.replace("\\", "/").lstrip("/")
            if not rel_path or rel_path.startswith(".."):
                rel_path = None
        if rel_path is None:
            return Capture(False, "outside the repository")

        text, read_via = self._read_back(path, raw_path, task_id)
        if text is None:
            key = self.store.register_repo(root)
            self.store.record_unreadable(key, {"task": task_id or "local", "path": rel_path, "ts": utc_now()})
            return Capture(False, "file could not be read back on this backend")

        reason = skip_reason(path, rel_path=rel_path, size=len(text.encode("utf-8")), text=text)
        if reason:
            return Capture(False, reason)

        redacted_full = self.redactor(text)
        redacted_lines = redacted_full.splitlines()
        lines = text.splitlines()
        key = self.store.register_repo(root)
        signals: list[Signal] = []
        skipped: list[str] = []

        for block in blocks[:MAX_BLOCKS]:
            if not block.added:
                continue
            if tool == "write_file":
                expected = block.raw if block.raw is not None else "\n".join(block.added)
                landed = content_hash(text) in {
                    content_hash(expected),
                    content_hash(expected + "\n"),
                }
                if not landed:
                    skipped.append(
                        f"{rel_path}: the file on disk is not what the tool wrote, so it did not land"
                    )
                    continue
                starts = [0]
            else:
                starts = find_all_exact(lines, block.added, limit=MAX_OCCURRENCES)
            if not starts:
                skipped.append(f"{rel_path}: block not found after the write, so it did not land")
                continue

            hunk = redact_lines(block.added, self.redactor) or []
            too_big = hunk_too_big(hunk)
            if too_big:
                skipped.append(f"{rel_path}: {too_big}")
                continue
            pre = None if block.pre is None else redact_lines(block.pre, self.redactor)

            for index, start in enumerate(starts, start=1):
                if tool == "write_file":
                    before_ctx: list[str] = []
                    after_ctx: list[str] = []
                else:
                    before_ctx = redacted_lines[max(0, start - CONTEXT_LINES) : start]
                    after_ctx = redacted_lines[start + len(block.added) :][:CONTEXT_LINES]
                signal = Signal(
                    id=new_signal_id(),
                    ts=utc_now(),
                    tool=tool,
                    path=rel_path,
                    content_sha256=content_hash(text),
                    hunk=hunk,
                    before_ctx=before_ctx,
                    after_ctx=after_ctx,
                    pre_hunk=pre,
                    start_line=start + 1,
                    total_lines=count_lines(text),
                    size=len(text.encode("utf-8")),
                    read_via=read_via,
                    occurrence=index,
                    occurrences=len(starts),
                    session_id=session_id,
                    turn_id=turn_id,
                )
                self.store.append(key, signal)
                signals.append(signal)

        self._read_cache.pop(rel_path, None)
        if not signals:
            reason = skipped[0] if skipped else "nothing written"
            return Capture(False, reason, skipped=skipped)
        return Capture(True, signals=signals, skipped=skipped)

    # -- classification ---------------------------------------------------
    def _reader(self, repo_root: Path):
        def read(rel_path: str) -> str | None:
            try:
                path = (repo_root / rel_path).resolve()
                stat = path.stat()
            except OSError:
                return None
            cached = self._read_cache.get(rel_path)
            if cached is not None and cached[0] == stat.st_mtime:
                return cached[1]
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            # Redact the same way the stored blocks were redacted, so a secret the
            # user left untouched cannot read as an edit.
            value = self.redactor(text)
            self._read_cache[rel_path] = (stat.st_mtime, value)
            return value

        return read

    def scan(self, repo_root: str | Path) -> Scan:
        root = Path(repo_root).resolve()
        key = repo_key(root)
        signals = self.store.rows(key)
        verdicts = classify_signals(signals, self._reader(root)) if signals else []
        return Scan(root, key, signals, verdicts)

    def repo_for(self, path: str | Path) -> Path | None:
        return find_repo_root(path)

    # -- the context file --------------------------------------------------
    def target(self, repo_root: str | Path, cwd: str | Path | None = None) -> contextfile.Target:
        return contextfile.resolve_target(Path(repo_root).resolve(), cwd)

    def entries(self, repo_root: str | Path, cwd: str | Path | None = None) -> list[str]:
        return contextfile.read_entries(self.target(repo_root, cwd).path)

    # -- learn --------------------------------------------------------------
    def proposal(
        self, repo_root: str | Path, cwd: str | Path | None = None
    ) -> tuple[list[dict[str, str]], int]:
        """Distillation messages for this repository, and how many examples they carry."""
        root = Path(repo_root).resolve()
        scan = self.scan(root)
        pairs = distill.select_pairs(scan.verdicts)
        if not pairs:
            return [], 0
        return distill.build_messages(pairs, self.entries(root, cwd)), len(pairs)

    def stage(
        self, repo_root: str | Path, learned: list[str], cwd: str | Path | None = None
    ) -> tuple[str, str, Path]:
        """Render the merge and remember it; returns ``(before, after, target path)``."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        target = self.target(root, cwd)
        existing = target.path.read_text(encoding="utf-8") if target.exists else None
        after = contextfile.render(existing, learned, self.store.last_generated(key))
        self.store.set_pending(
            key,
            {
                "learned": list(learned),
                "target": str(target.path),
                "before_hash": content_hash(existing or ""),
                "ts": utc_now(),
            },
        )
        return existing or "", after, target.path

    def confirm(self, repo_root: str | Path, cwd: str | Path | None = None) -> Path | None:
        """Apply the staged proposal, preserving edits made since staging."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        payload = self.store.pending(key)
        if not payload:
            return None
        learned = [str(item) for item in (payload.get("learned") or [])]
        target_path = Path(str(payload.get("target") or "")) if payload.get("target") else None
        if target_path is None:
            target_path = self.target(root, cwd).path
        try:
            existing = target_path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        rendered = contextfile.render(existing, learned, self.store.last_generated(key))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(rendered, encoding="utf-8")
        self.store.set_last_generated(key, learned)
        self.store.clear_pending(key)
        return target_path

    def reset(
        self, repo_root: str | Path, *, confirmed: bool, cwd: str | Path | None = None
    ) -> tuple[int, int, str]:
        """Clear signals and learned rules; returns ``(signals, kept_lines, state)``."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        target = self.target(root, cwd)
        last = self.store.last_generated(key)
        generated = {contextfile.clean_entry(line).casefold() for line in last}
        current = contextfile.read_entries(target.path)
        kept = [entry for entry in current if entry.casefold() not in generated]
        if not confirmed:
            return len(self.store.rows(key)), len(kept), "planned"
        signals = self.store.clear(key)
        self.store.clear_pending(key)
        self.store.clear_unreadable(key)
        if target.exists:
            existing = target.path.read_text(encoding="utf-8")
            target.path.write_text(contextfile.render(existing, [], last), encoding="utf-8")
        self.store.set_last_generated(key, [])
        return signals, len(kept), "done"

    # -- reporting ----------------------------------------------------------
    def status(
        self,
        repo_root: str | Path,
        *,
        cwd: str | Path | None = None,
        max_pairs: int = STATUS_PAIRS,
        capture_note: str = "",
    ) -> str:
        root = Path(repo_root).resolve()
        scan = self.scan(root)
        counts = scan.counts
        key = scan.key
        meta = self.store.meta(key)
        target = self.target(root, cwd)
        entries = contextfile.read_entries(target.path)

        lines = [f"House style — {root}"]
        if self.capture_enabled:
            lines.append("  capture: on (redaction: the host's own redactor)")
        else:
            lines.append(f"  capture: OFF — {capture_note or 'this Hermes build has no host redactor'}")
        lines.append(f"  enabled for this project: {'no (/house-style off)' if meta.get('off') else 'yes'}")
        state = "exists" if target.exists else "will be created"
        rel_target = relative_to_root(root, target.path) or target.path.name
        lines.append(f"  context file: {rel_target} ({target.kind}, {state})")
        lines.append(f"  rules: {len(entries)}")

        by_via: dict[str, int] = {}
        for signal in scan.signals:
            by_via[signal.read_via] = by_via.get(signal.read_via, 0) + 1
        via = ", ".join(f"{name} {count}" for name, count in sorted(by_via.items())) or "none"
        lines.append(f"  signals: {len(scan.signals)} ({via})")
        lines.append(
            "  verdicts: "
            f"{counts.get(KEPT, 0)} kept · {counts.get('edited', 0)} edited · "
            f"{counts.get('reverted', 0)} reverted"
        )

        # Files a backend would not let us read are reported for this project and for
        # every other one we track: a Docker or SSH session can be filed under the
        # directory it was launched from, and a silent drop there would be a gap.
        unreadable: list[tuple[str, str, str]] = []
        for row in self.store.unreadable(key):
            unreadable.append(("", str(row.get("task") or "local"), str(row.get("path") or "?")))
        for other_key, _ in self.store.tracked_repos():
            if other_key == key:
                continue
            other_root = str(self.store.meta(other_key).get("repo") or other_key)
            label = Path(other_root).name or other_root
            for row in self.store.unreadable(other_key):
                unreadable.append((label, str(row.get("task") or "local"), str(row.get("path") or "?")))
        if unreadable:
            grouped: dict[tuple[str, str], list[str]] = {}
            for label, backend, path in unreadable:
                grouped.setdefault((label, backend), []).append(path)
            for (label, backend), paths in list(grouped.items())[:6]:
                shown = ", ".join(paths[-3:])
                where = f" in {label}" if label else ""
                lines.append(f"  could not read on backend {backend}{where}: {len(paths)} file(s): {shown}")

        if self.store.pending(key):
            lines.append("  pending learn proposal: yes — run /house-style learn confirm")

        edited = [v for v in scan.verdicts if v.status == "edited" and (v.before or v.after)]
        if edited:
            lines.append("")
            lines.append(f"Newest {min(len(edited), max_pairs)} edited pair(s):")
            for verdict in edited[-max_pairs:][::-1]:
                lines.append(f"  {verdict.path}")
                for side, block in (("agent", verdict.before), ("user ", verdict.after)):
                    if not block:
                        continue
                    for row in block[:6]:
                        lines.append(f"    {side} | {row}")
                    if len(block) > 6:
                        lines.append(f"    {side} | ...")

        others = [other for other, _ in self.store.tracked_repos() if other != key]
        if others:
            lines.append("")
            lines.append(f"Other tracked projects: {len(others)}")
        return "\n".join(lines)

    def export(self, repo_root: str | Path, cwd: str | Path | None = None) -> str:
        target = self.target(repo_root, cwd)
        entries = contextfile.read_entries(target.path)
        if not entries:
            return f"No house-style rules in {target.path.name} yet."
        body = "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
        return f"## House style\n\n{body}"
