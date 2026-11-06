"""The harness-agnostic engine: capture, classify, report, propose.

Everything that touches a repository or a store lives here. A harness adapter only
has to say *where* the repo is, *what* the tool wrote, and *who* does the redaction
and the model call.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import distill, prefs
from .filters import (
    CONTEXT_LINES,
    MAX_FILE_BYTES,
    count_lines,
    hunk_too_big,
    skip_reason,
)
from .gitio import find_repo_root, relative_to_root
from .hunks import classify_signals, find_exact
from .models import KEPT, Signal, Verdict, content_hash, new_signal_id, utc_now
from .redaction import Redactor, make_redactor, redact_lines
from .store import Store, repo_key

#: Relative path of the preferences file inside a project.
PREFS_RELPATH = ".hermes/house-style.md"

#: Characters of the preferences file injected into a first turn.
INJECT_MAX_CHARS = 2000

#: Newest edited pairs shown by ``status``.
STATUS_PAIRS = 10


@dataclass
class Capture:
    """Outcome of one capture attempt."""

    stored: bool
    reason: str = ""
    signal: Signal | None = None


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
        redactor: Redactor | None = None,
        is_protected: Callable[[str], bool] | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.store = Store(base_dir)
        self.redactor = make_redactor(redactor)
        self.is_protected = is_protected
        self.max_file_bytes = max_file_bytes
        self._read_cache: dict[str, tuple[float, str | None]] = {}

    # -- capture ---------------------------------------------------------
    def capture(
        self,
        *,
        repo_root: Path,
        file_path: Path,
        tool: str,
        written_text: str,
        pre_text: str | None = None,
        session_id: str = "",
        turn_id: str = "",
    ) -> Capture:
        """Record what *tool* wrote at *file_path*.

        ``written_text`` is the block the tool introduced (the whole file for
        ``write_file``, the replacement text for ``patch``). The file is read back
        from disk so the stored hashes and context describe what is actually there.
        """
        try:
            repo_root = Path(repo_root).resolve()
        except OSError:
            return Capture(False, "repository path unavailable")
        rel_path = relative_to_root(repo_root, Path(file_path))
        if rel_path is None:
            return Capture(False, "outside the repository")
        try:
            size = Path(file_path).stat().st_size
        except OSError:
            return Capture(False, "file is gone")

        raw_text = None
        try:
            raw_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_text = None
        if raw_text is None:
            return Capture(False, "file is unreadable")

        reason = skip_reason(
            Path(file_path),
            rel_path=rel_path,
            size=size,
            text=raw_text,
            is_protected=self.is_protected,
        )
        if reason:
            return Capture(False, reason)

        redacted_full = self.redactor(raw_text)
        if tool == "write_file":
            hunk = redacted_full.splitlines()
            before_ctx: list[str] = []
            after_ctx: list[str] = []
            start_line = 1
        else:
            hunk = redact_lines((written_text or "").splitlines(), self.redactor) or []
            located = find_exact(raw_text.splitlines(), (written_text or "").splitlines())
            if located is None:
                before_ctx, after_ctx, start_line = [], [], 0
            else:
                redacted_lines = redacted_full.splitlines()
                start_line = located.start + 1
                before_ctx = redacted_lines[max(0, located.start - CONTEXT_LINES) : located.start]
                after_ctx = redacted_lines[located.start + len(located.lines) :][:CONTEXT_LINES]

        if not hunk:
            return Capture(False, "nothing written")
        too_big = hunk_too_big(hunk)
        if too_big:
            return Capture(False, too_big)

        signal = Signal(
            id=new_signal_id(),
            ts=utc_now(),
            tool=tool,
            path=rel_path,
            content_sha256=content_hash(raw_text),
            hunk=hunk,
            before_ctx=before_ctx,
            after_ctx=after_ctx,
            pre_hunk=None if pre_text is None else redact_lines(pre_text.splitlines(), self.redactor),
            start_line=start_line,
            total_lines=count_lines(raw_text),
            size=size,
            session_id=session_id,
            turn_id=turn_id,
        )
        key = self.store.register_repo(repo_root)
        self.store.append(key, signal)
        self._read_cache.pop(rel_path, None)
        return Capture(True, signal=signal)

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

    # -- preferences file --------------------------------------------------
    def prefs_path(self, repo_root: str | Path) -> Path:
        return Path(repo_root) / PREFS_RELPATH

    def injection(self, repo_root: str | Path) -> str | None:
        """Ephemeral context block for the first turn of a session, or ``None``."""
        root = Path(repo_root).resolve()
        doc = prefs.read_prefs(self.prefs_path(root))
        if not doc.exists:
            return None
        entries = doc.entries()
        if not entries:
            return None
        body = "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
        block = (
            "## House style for this project\n"
            "\n"
            f"The user's preferences for {root.name}, learned from how they corrected earlier "
            "edits. Follow them unless this request says otherwise.\n"
            "\n"
            f"{body}\n"
        )
        text, truncated = prefs.cap(block, INJECT_MAX_CHARS)
        if truncated:
            text += f"\n(truncated; full file: {PREFS_RELPATH})"
        return text

    # -- learn --------------------------------------------------------------
    def proposal(self, repo_root: str | Path) -> tuple[list[dict[str, str]], int]:
        """Build the distillation messages for this repository.

        Returns ``(messages, pair_count)``; ``pair_count == 0`` means nothing to learn
        from.
        """
        root = Path(repo_root).resolve()
        scan = self.scan(root)
        doc = prefs.read_prefs(self.prefs_path(root))
        pairs = distill.select_pairs(scan.verdicts)
        if not pairs:
            return [], 0
        return distill.build_messages(pairs, doc.entries()), len(pairs)

    def stage(self, repo_root: str | Path, learned: Sequence[str]) -> tuple[str, str]:
        """Render the merge and remember it; returns ``(before, after)``."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        path = self.prefs_path(root)
        doc = prefs.read_prefs(path)
        before = doc.text
        after = prefs.render(doc, learned, self.store.last_generated(key))
        self.store.set_pending(
            key,
            {
                "learned": list(learned),
                "before_hash": content_hash(before),
                "ts": utc_now(),
            },
        )
        return before, after

    def confirm(self, repo_root: str | Path) -> str | None:
        """Apply the staged proposal, preserving edits made since staging."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        payload = self.store.pending(key)
        if not payload:
            return None
        learned = [str(x) for x in (payload.get("learned") or [])]
        path = self.prefs_path(root)
        doc = prefs.read_prefs(path)
        rendered = prefs.render(doc, learned, self.store.last_generated(key))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(path)
        self.store.set_last_generated(key, learned)
        self.store.clear_pending(key)
        return rendered

    def reset(self, repo_root: str | Path, *, confirmed: bool) -> tuple[int, int, str]:
        """Clear signals and learned entries; returns ``(signals, kept, summary)``."""
        root = Path(repo_root).resolve()
        key = self.store.register_repo(root)
        path = self.prefs_path(root)
        doc = prefs.read_prefs(path)
        last = self.store.last_generated(key)
        generated = {prefs.clean_entry(line).casefold() for line in last}
        kept = [
            prefs.clean_entry(line)
            for line in doc.inside_block
            if prefs.clean_entry(line) and prefs.clean_entry(line).casefold() not in generated
        ]
        if not confirmed:
            return len(self.store.rows(key)), len(kept), "planned"
        signals = self.store.clear(key)
        self.store.clear_pending(key)
        if doc.exists and doc.has_block:
            rendered = prefs.render(doc, [], last)
            path.write_text(rendered, encoding="utf-8")
        self.store.set_last_generated(key, [])
        return signals, len(kept), "done"

    # -- reporting ----------------------------------------------------------
    def status(self, repo_root: str | Path, *, max_pairs: int = STATUS_PAIRS) -> str:
        root = Path(repo_root).resolve()
        scan = self.scan(root)
        counts = scan.counts
        doc = prefs.read_prefs(self.prefs_path(root))
        key = scan.key
        meta = self.store.meta(key)

        lines = [f"House style — {root}"]
        lines.append(f"  enabled: {'no (off)' if meta.get('off') else 'yes'}")
        lines.append(f"  signals: {len(scan.signals)}")
        lines.append(
            "  verdicts: "
            f"{counts.get(KEPT, 0)} kept · {counts.get('edited', 0)} edited · "
            f"{counts.get('reverted', 0)} reverted"
        )
        summary = prefs.describe(doc)
        lines.append(f"  preferences: {summary or 'none yet'} ({PREFS_RELPATH})")
        pending = self.store.pending(key)
        if pending:
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
        others = [
            (other_key, meta_path) for other_key, meta_path in self.store.tracked_repos() if other_key != key
        ]
        if others:
            lines.append("")
            lines.append(f"Other tracked projects: {len(others)}")
        return "\n".join(lines)

    def export(self, repo_root: str | Path) -> str:
        doc = prefs.read_prefs(self.prefs_path(Path(repo_root).resolve()))
        if not doc.exists:
            return f"No {PREFS_RELPATH} in this project yet."
        return doc.text
