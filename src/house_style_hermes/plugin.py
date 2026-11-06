"""Hermes adapter: hooks, commands and the host seams house-style needs.

This module is the only place that knows about Hermes. The behaviour lives in
``house_style_core``; here we only answer five questions the core cannot: which
repository the user is working in, what a tool wrote, how to read a file the host
cannot see, who redacts, and who makes the model call.

Everything it uses is a documented ``ctx`` method: ``redact_text``, ``session_cwd``,
``dispatch_tool``, ``llm``, ``register_hook`` and ``register_command``. No Hermes
module is imported, and no private seam is used or fallen back to.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from house_style_core import distill, parse_patch
from house_style_core.engine import Block, Engine
from house_style_core.gitio import find_repo_root, relative_to_root

logger = logging.getLogger(__name__)

PLUGIN_NAME = "house-style"

#: File tools whose writes are captured.
CAPTURE_TOOLS = ("write_file", "patch")

#: Seconds between background classification passes for one repository.
SCAN_MIN_INTERVAL = 30.0

#: Timeout for the distillation call (a reasoning model needs room).
LEARN_TIMEOUT = 180.0

#: Output budget for the distillation call; hidden reasoning spends part of it.
LEARN_MAX_TOKENS = 2048

#: A write whose result is not one of these did not land.
OK_STATUSES = ("", "ok", "success", "completed")

#: Line ceiling for a read through the tool surface (the ``read_file`` default).
TOOL_READ_LINES = 2000

NO_REDACTOR = (
    "this Hermes build has no ctx.redact_text(), so nothing is recorded. "
    "house-style needs the core plugin-redaction seam (see the README)."
)

_engine: Engine | None = None
_engine_lock = threading.Lock()
_ctx: Any = None
_last_scan: dict[str, float] = {}


# ---------------------------------------------------------------------------
# host seams — public ctx methods only
# ---------------------------------------------------------------------------
def _plugin_data_dir() -> Path:
    """The host's own plugin-data directory. No fallback: a refusal is reported."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _host_redact() -> Any:
    """The host's redactor, or ``None`` on a build without the seam."""
    method = getattr(_ctx, "redact_text", None)
    if not callable(method):
        return None

    def redact(text: str) -> str:
        result = method(text)
        return result if isinstance(result, str) else text

    return redact


def _session_cwd() -> Path:
    """The working directory this session was started in."""
    try:
        raw = _ctx.session_cwd() if _ctx is not None else ""
    except Exception:
        raw = ""
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return Path(os.getcwd())


def _read_remote(raw_path: str, task_id: str) -> str | None:
    """Read a file through the tool surface, which is what reaches a remote backend."""
    dispatch = getattr(_ctx, "dispatch_tool", None)
    if not callable(dispatch):
        return None
    try:
        raw = dispatch("read_file", {"path": raw_path, "limit": TOOL_READ_LINES})
    except Exception:
        logger.debug("house-style: read_file dispatch failed", exc_info=True)
        return None
    return _strip_line_numbers(raw)


def _is_numbered(line: str) -> bool:
    head, sep, _ = line.partition("|")
    return bool(sep) and head.strip().isdigit()


def _strip_line_numbers(raw: Any) -> str | None:
    """Turn ``read_file``'s ``LINE|CONTENT`` listing back into plain text.

    Anything that is not a paginated listing — an error string, a refusal, an empty
    reply — is ``None``: no file text was read, and capture must say so rather than
    store an error message as if it were the file.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    lines = raw.splitlines()
    numbered = [line for line in lines if _is_numbered(line)]
    if not numbered or len(numbered) < max(1, int(len(lines) * 0.8)):
        return None
    out = [line.partition("|")[2] if _is_numbered(line) else line for line in lines]
    return "\n".join(out) + ("\n" if raw.endswith("\n") else "")


def _engine_for(base_dir: Path) -> Engine:
    """One engine per plugin-data directory (a profile switch rebuilds it)."""
    global _engine
    with _engine_lock:
        redact = _host_redact()
        if _engine is None or _engine.store.base != base_dir or _engine.redact is not redact:
            _engine = Engine(base_dir, redact=redact, read_remote=_read_remote)
        return _engine


def engine() -> Engine:
    """Engine for the active profile; raises when the host gives no data dir."""
    return _engine_for(_plugin_data_dir())


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _resolve_tool_path(raw: str) -> Path:
    """A tool's ``path`` argument, resolved against the session working directory."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return _session_cwd() / candidate


def _repo_root(path: Path) -> Path | None:
    """Repository root for a path, or the session's logical root on a remote backend.

    A file the host cannot see lives on a backend, where ``.git`` is not visible from
    here; the session working directory (or the repository it sits in) is then the best
    root available. Status shows per-signal how each file was read, so this is never a
    silent substitution.
    """
    found = find_repo_root(path)
    if found is not None:
        return found
    try:
        visible = path.exists() or path.parent.exists()
    except OSError:
        visible = False
    if visible:
        return None
    cwd = _session_cwd()
    return find_repo_root(cwd) or cwd


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
def _blocks_for(tool: str, args: dict) -> list[tuple[str, list[Block]]]:
    """The blocks a tool call introduced, grouped by the raw path they belong to."""
    if tool == "write_file":
        content = args.get("content")
        path = args.get("path")
        if isinstance(content, str) and content.strip() and isinstance(path, str) and path:
            return [(path, [Block(content.splitlines())])]
        return []

    mode = str(args.get("mode") or "replace").strip().lower()
    if mode == "patch" or isinstance(args.get("patch"), str):
        parsed = parse_patch(str(args.get("patch") or ""))
        grouped: dict[str, list[Block]] = {}
        for hunk in parsed.hunks:
            if hunk.added:
                grouped.setdefault(hunk.path, []).append(Block(hunk.added, hunk.removed or None))
        return list(grouped.items())

    path = args.get("path")
    new_text = args.get("new_string")
    old_text = args.get("old_string")
    if not isinstance(path, str) or not path or not isinstance(new_text, str) or not new_text.strip():
        return []
    pre = old_text.splitlines() if isinstance(old_text, str) and old_text else None
    return [(path, [Block(new_text.splitlines(), pre)])]


def _logical_rel_path(raw_path: str, root: Path, path: Path) -> str:
    """Path relative to the repository, or the backend's own path for a remote file.

    A file on a remote backend has no host-visible path under the repository, so its
    path as the tool saw it (leading slashes removed) is the stable identity to key on.
    """
    return relative_to_root(root, path) or raw_path.replace("\\", "/").lstrip("/")


def _capture_tool_call(
    tool: str, args: dict, status: str, task_id: str, session_id: str, turn_id: str
) -> None:
    if status not in OK_STATUSES:
        return
    eng = engine()
    if not eng.capture_enabled:
        return  # /house-style status explains why
    for raw_path, blocks in _blocks_for(tool, args):
        if not blocks:
            continue
        path = _resolve_tool_path(raw_path)
        root = _repo_root(path)
        if root is None:
            logger.debug("house-style: %s is outside a git repository", raw_path)
            continue
        key = eng.store.register_repo(root)
        if eng.store.is_off(key):
            continue
        capture = eng.capture(
            repo_root=root,
            file_path=path,
            raw_path=raw_path,
            tool=tool,
            blocks=blocks,
            rel_path=_logical_rel_path(raw_path, root, path),
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        if not capture.stored:
            logger.debug("house-style: not captured: %s", capture.reason)


def _maybe_scan(force: bool = False) -> None:
    """Classify stored blocks for the current repository, at most every 30 s."""
    cwd = _session_cwd()
    root = find_repo_root(cwd) or cwd
    now = time.monotonic()
    key = str(root)
    if not force and (now - _last_scan.get(key, 0.0)) < SCAN_MIN_INTERVAL:
        return
    _last_scan[key] = now
    try:
        engine().scan(root)
    except Exception:
        logger.debug("house-style: scan failed", exc_info=True)


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------
def on_post_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    result: Any = None,
    status: str = "",
    task_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    **kwargs: Any,
) -> None:
    """Observer: record what ``write_file`` and ``patch`` wrote. Returns nothing."""
    if tool_name not in CAPTURE_TOOLS:
        return None
    try:
        _capture_tool_call(
            tool_name, args or {}, status or "", task_id or "", session_id or "", turn_id or ""
        )
    except Exception:
        logger.debug("house-style: capture failed", exc_info=True)
    return None


def on_session_start(session_id: str = "", **kwargs: Any) -> None:
    """New session: catch up on what the user did with earlier agent edits."""
    try:
        _maybe_scan(force=True)
    except Exception:
        logger.debug("house-style: session-start scan failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# slash command
# ---------------------------------------------------------------------------
_USAGE = (
    "Usage: /house-style [status|scan|learn|learn confirm|reset [confirm]|off|on|export]\n"
    "  status    capture state, rule counts and the newest edited pairs\n"
    "  scan      re-check what happened to the edits it recorded\n"
    "  learn     distil rules with the model and show the diff\n"
    "  reset     clear signals and learned rules (add confirm to apply)\n"
    "  off / on  stop or resume recording this project\n"
    "  export    print the rules as markdown"
)


def _format_counts(counts: dict[str, int]) -> str:
    return (
        f"{counts.get('kept', 0)} kept, {counts.get('edited', 0)} edited, "
        f"{counts.get('reverted', 0)} reverted"
    )


def _relative_target(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def _learn(root: Path, eng: Engine, confirm: bool) -> str:
    cwd = _session_cwd()
    if confirm:
        target = eng.confirm(root, cwd)
        if target is None:
            return "No proposal is waiting. Run /house-style learn first."
        return (
            f"Wrote {_relative_target(root, target)}. It is a normal project file now: "
            "edit it, delete lines from it, or delete it whenever you like."
        )

    messages, pair_count = eng.proposal(root, cwd)
    if pair_count == 0:
        return (
            "Nothing to learn from yet. Edit some code the agent wrote, then run "
            "/house-style scan (or just work on: it checks at each new session)."
        )
    if getattr(_ctx, "llm", None) is None:
        return "This Hermes build does not expose a model to plugins, so nothing was learned."

    result = _ctx.llm.complete(
        messages=messages,
        max_tokens=LEARN_MAX_TOKENS,
        timeout=LEARN_TIMEOUT,
        purpose="house-style learn",
    )
    learned = distill.parse_response(getattr(result, "text", "") or "")
    if not learned:
        return "The model returned no usable rules; nothing was staged."

    before, after, target = eng.stage(root, learned, cwd)
    diff = distill.diff_preview(before, after)
    cost = getattr(getattr(result, "usage", None), "cost_usd", None)
    spent = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""
    model = getattr(result, "model", "") or "the session model"
    return (
        f"Staged {len(learned)} rule(s) from {pair_count} example(s) "
        f"using {model}{spent}, to go into {_relative_target(root, target)}.\n\n{diff}\n\n"
        "Run /house-style learn confirm to write it, or ignore this to leave the file alone."
    )


def _reset(root: Path, eng: Engine, confirmed: bool) -> str:
    signals, kept, state = eng.reset(root, confirmed=confirmed, cwd=_session_cwd())
    if state == "planned":
        return (
            f"Would clear {signals} stored edit(s) and every learned rule"
            + (f", keeping {kept} line(s) you wrote" if kept else "")
            + ".\nRun /house-style reset confirm to do it."
        )
    return (
        f"Cleared {signals} stored edit(s) and the learned rules"
        + (f"; kept {kept} line(s) you wrote" if kept else "")
        + "."
    )


def handle_command(raw_args: str = "", **kwargs: Any) -> str:
    """``/house-style`` entry point, shared by the classic CLI, the TUI and the gateway."""
    text = (raw_args or "").strip()
    sub, _, rest = text.partition(" ")
    sub = sub.strip().lower()
    rest = rest.strip()
    if sub in ("", "status"):
        sub = "status"
    elif sub in ("help", "-h", "--help"):
        return _USAGE
    elif sub == "learn" and rest.lower() == "confirm":
        sub, rest = "learn-confirm", ""
    elif sub == "reset" and rest.lower() == "confirm":
        sub, rest = "reset-confirm", ""

    cwd = _session_cwd()
    root = find_repo_root(cwd)
    if root is None:
        return (
            "house-style: this working directory is not inside a git repository, so there "
            "is nothing to track."
        )
    try:
        eng = engine()
    except Exception as exc:
        return f"house-style: plugin data directory unavailable ({exc})."

    try:
        if sub == "status":
            return eng.status(root, cwd=cwd, capture_note=NO_REDACTOR)
        if sub == "scan":
            if not eng.capture_enabled:
                return f"house-style: nothing was scanned — {NO_REDACTOR}"
            scan = eng.scan(root)
            return (
                f"Scanned {len(scan.signals)} recorded edit(s) in {root.name}: {_format_counts(scan.counts)}."
            )
        if sub == "learn":
            if not eng.capture_enabled:
                return f"house-style: nothing was learned — {NO_REDACTOR}"
            return _learn(root, eng, confirm=False)
        if sub == "learn-confirm":
            return _learn(root, eng, confirm=True)
        if sub == "reset":
            return _reset(root, eng, confirmed=False)
        if sub == "reset-confirm":
            return _reset(root, eng, confirmed=True)
        if sub == "off":
            eng.store.set_off(eng.store.register_repo(root), True)
            return f"house-style is off for {root.name}: new edits are not recorded."
        if sub == "on":
            eng.store.set_off(eng.store.register_repo(root), False)
            return f"house-style is on for {root.name}."
        if sub == "export":
            return eng.export(root, cwd)
        return "Unknown subcommand.\n\n" + _USAGE
    except Exception as exc:  # never raise into the operator's terminal
        logger.debug("house-style: command failed", exc_info=True)
        return f"house-style: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def register(ctx: Any) -> None:
    """Wire house-style into Hermes. Writes nothing."""
    global _ctx
    _ctx = ctx
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_command(
        "house-style",
        handle_command,
        description=(
            "Learn this project's house style from your corrections and write it into the "
            "project context file: status, scan, learn, reset, off, on, export"
        ),
        args_hint="status|scan|learn|reset|off|on|export",
    )
