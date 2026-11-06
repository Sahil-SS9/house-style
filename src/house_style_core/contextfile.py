"""Where the learned rules live: the project context file Hermes already loads.

There is no separate preferences file any more. The rules go into the file a session
would read anyway — ``.hermes.md``/``HERMES.md``, else ``AGENTS.md``, else
``CLAUDE.md``, else ``.cursorrules`` — inside a marked block, so:

* nothing shadows an existing context file that Hermes would otherwise load;
* the rules reach the model through Hermes's normal context loading, in the classic
  CLI, the TUI and every gateway surface, with no injection of our own;
* another harness reads the same file (Claude Code reads ``AGENTS.md`` too).

The block is the only region house-style rewrites. Everything outside it, and any line
the user added or reworded inside it, is preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

START_MARKER = "<!-- house-style:generated:start -->"
END_MARKER = "<!-- house-style:generated:end -->"
SECTION_TITLE = "## House style"

MAX_ENTRIES = 12
MAX_ENTRY_CHARS = 200

_NUMBERING = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: Content written when house-style has to create the context file itself.
NEW_FILE_HEADER = "# AGENTS.md\n\nProject instructions for AI coding agents.\n"

AGENTS_NAMES = ("AGENTS.override.md", "AGENTS.md", "agents.md")
HERMES_NAMES = (".hermes.md", "HERMES.md")
CLAUDE_NAMES = ("CLAUDE.md", "claude.md")


@dataclass
class Target:
    """The context file the rules belong in."""

    path: Path
    kind: str
    exists: bool


def _first(paths: list[Path]) -> Path | None:
    for path in paths:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _ancestors_to_root(cwd: Path, root: Path) -> list[Path]:
    """``cwd`` up to ``root`` inclusive, nearest first; ``[root]`` when cwd is outside."""
    try:
        here = cwd.resolve()
        base = root.resolve()
    except OSError:
        return [root]
    if here != base and base not in here.parents:
        return [base]
    chain = [here]
    while chain[-1] != base:
        chain.append(chain[-1].parent)
    return chain


def resolve_target(repo_root: str | Path, cwd: str | Path | None = None) -> Target:
    """The file Hermes would load for this working directory, or a new ``AGENTS.md``.

    The order and the per-directory names follow ``agent/prompt_builder.py``: the
    nearest ``.hermes.md``/``HERMES.md`` wins outright; otherwise the first
    ``AGENTS.md``-family file walking down from the git root; otherwise ``CLAUDE.md``;
    otherwise ``.cursorrules`` (or the first ``.cursor/rules/*.mdc``); otherwise a new
    ``AGENTS.md`` at the repository root.
    """
    root = Path(repo_root).resolve()
    here = Path(cwd).resolve() if cwd else root
    if here != root and root not in here.parents:
        here = root

    for directory in _ancestors_to_root(here, root):
        found = _first([directory / name for name in HERMES_NAMES])
        if found is not None:
            return Target(found, found.name, True)

    for directory in reversed(_ancestors_to_root(here, root)):
        found = _first([directory / name for name in AGENTS_NAMES])
        if found is not None:
            return Target(found, "AGENTS.md", True)

    found = _first([here / name for name in CLAUDE_NAMES])
    if found is not None:
        return Target(found, "CLAUDE.md", True)

    cursorrules = here / ".cursorrules"
    try:
        if cursorrules.is_file():
            return Target(cursorrules, ".cursorrules", True)
    except OSError:
        pass
    try:
        rules = sorted((here / ".cursor" / "rules").glob("*.mdc"))
    except OSError:
        rules = []
    if rules:
        return Target(rules[0], ".cursor/rules", True)

    return Target(root / "AGENTS.md", "AGENTS.md", False)


def block_lines(text: str) -> list[str]:
    """The raw lines between the markers (empty when there is no block)."""
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end < 0 or end <= start:
        return []
    body = text[start + len(START_MARKER) : end]
    return [line for line in body.splitlines() if line.strip()]


def clean_entry(raw: str) -> str:
    """Strip list numbering, bullets and surrounding whitespace from one entry."""
    line = _NUMBERING.sub("", raw.strip()).strip()
    if not line or line.startswith("<!--"):
        return ""
    if len(line) > MAX_ENTRY_CHARS:
        line = line[: MAX_ENTRY_CHARS - 1].rstrip() + "\u2026"
    return line


_NUMBERING = __import__("re").compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def normalise(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        clean = clean_entry(entry)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def entries_from_text(text: str) -> list[str]:
    """Current rules in a block, without numbering."""
    return normalise(block_lines(text or ""))


def read_entries(path: str | Path) -> list[str]:
    """Current rules in a file's block, without numbering."""
    try:
        return entries_from_text(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def _render_block(entries: list[str]) -> str:
    numbered = "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
    return f"{START_MARKER}\n{numbered}\n{END_MARKER}"


def render(existing: str | None, learned: list[str], last_generated: list[str]) -> str:
    """Merge *learned* into the file's block, preserving everything the user owns."""
    text = existing or ""
    prior = {clean_entry(line).casefold() for line in last_generated if clean_entry(line)}
    preserved: list[str] = []
    for raw in block_lines(text):
        clean = clean_entry(raw)
        if not clean or clean.casefold() in prior:
            continue
        preserved.append(clean)

    merged = normalise(list(learned) + preserved)
    has_block = START_MARKER in text and END_MARKER in text

    if not merged:
        if has_block:
            return _remove_section(text)
        return text if text else NEW_FILE_HEADER

    block = _render_block(merged)
    if has_block:
        start = text.index(START_MARKER)
        end = text.index(END_MARKER) + len(END_MARKER)
        return text[:start] + block + text[end:]
    if not text.strip():
        text = NEW_FILE_HEADER
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return f"{text}{separator}{SECTION_TITLE}\n\n{block}\n"


def _remove_section(text: str) -> str:
    """Take the whole managed section out, leaving the rest of the file untouched."""
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end <= start:
        return text
    head = text[:start]
    tail = text[end + len(END_MARKER) :]
    # The heading immediately above the block goes with it.
    lines = head.split("\n")
    while lines and (not lines[-1].strip() or lines[-1].strip() == SECTION_TITLE):
        removed = lines.pop()
        if removed.strip() == SECTION_TITLE:
            break
    head = "\n".join(lines).rstrip("\n")
    combined = head + ("\n" + tail.lstrip("\n") if tail.strip() else "\n")
    return combined if combined.strip() else ""


def cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Trim *text* to *max_chars* on a line boundary; returns ``(text, truncated)``."""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    return cut.rstrip() + "\n...", True
