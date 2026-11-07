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

import json
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
_redact_source: Any = None
_engine_lock = threading.Lock()
_ctx: Any = None
_last_scan: dict[str, float] = {}

#: The working directory each session reported when it started. A ``post_tool_call``
#: hook runs without the session's own context on some builds, where
#: ``ctx.session_cwd()`` then answers with something other than the session's directory;
#: remembering what the session itself reported keeps capture keyed the same way the
#: commands are.
_session_cwds: dict[str, str] = {}

#: Working directory each backend task reported, keyed by task id.
_task_cwds: dict[str, str] = {}

#: Last remote-read failure, so status can say what the host answered.
_last_read_error: str = ""


# ---------------------------------------------------------------------------
# host seams — public ctx methods only
# ---------------------------------------------------------------------------
def _plugin_data_dir() -> Path:
    """The host's own plugin-data directory. No fallback: a refusal is reported."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _host_redact() -> Any:
    """The host's redactor, or ``None`` on a build without the seam.

    ``ctx.redact_text`` raises on internal failure rather than returning the text
    unchanged, and that is the behaviour we want: the exception propagates so the
    engine refuses to store anything it could not scrub.
    """
    method = getattr(_ctx, "redact_text", None)
    if not callable(method):
        return None

    def redact(text: str) -> str:
        result = method(text)
        if not isinstance(result, str):
            raise TypeError(f"ctx.redact_text returned {type(result).__name__}, not str")
        return result

    return redact


def _session_cwd(session_id: str = "") -> Path:
    """The working directory this session was started in.

    ``session_id`` is preferred when we have seen that session report its own directory;
    inside a hook the host may not have the session's context, and its answer would then
    be the launch directory instead.
    """
    remembered = _session_cwds.get(session_id or "")
    if remembered:
        return Path(remembered)
    try:
        raw = _ctx.session_cwd() if _ctx is not None else ""
    except Exception:
        raw = ""
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return Path(os.getcwd())


def _remember_cwd(session_id: str) -> None:
    """Record the session's own directory while its context is available."""
    try:
        raw = _ctx.session_cwd() if _ctx is not None else ""
    except Exception:
        return
    if isinstance(raw, str) and raw.strip() and session_id:
        if len(_session_cwds) > 500:
            _session_cwds.clear()
        _session_cwds[session_id] = raw.strip()


def _read_remote(raw_path: str, task_id: str, abs_path: str = "") -> str | None:
    """Read a file through the tool surface, in the task that wrote it.

    ``task_id`` matters: ``registry.dispatch`` hands it to the read handler, which
    resolves relative paths against that task's own backend working directory and reads
    through that backend's environment. Without it the read runs against whatever
    environment the default task has — a host one, for a Docker or SSH session.
    """
    global _last_read_error
    dispatch = getattr(_ctx, "dispatch_tool", None)
    if not callable(dispatch):
        _last_read_error = "this build has no ctx.dispatch_tool"
        return None
    tid = task_id or "default"
    attempts: list[str] = []
    for candidate in (raw_path, abs_path):
        if not candidate:
            continue
        try:
            raw = dispatch("read_file", {"path": candidate, "limit": TOOL_READ_LINES}, task_id=tid)
        except Exception as exc:
            attempts.append(f"{candidate}: dispatch raised {type(exc).__name__}: {exc}")
            continue
        text = _strip_line_numbers(raw)
        if text is not None:
            _last_read_error = ""
            return text
        preview = " ".join(str(raw).split())[:160]
        attempts.append(f"{candidate}: {preview}")
    _last_read_error = " | ".join(attempts) or "no path to try"
    logger.info("house-style: remote read failed (task=%s): %s", tid, _last_read_error[:400])
    return None


def _unwrap(raw: Any, keys: tuple[str, ...]) -> str | None:
    """The text inside a tool result, whatever envelope ``registry.dispatch`` gave it: the bare
    output, a JSON string, or a JSON object carrying it under one of *keys*. None if there is none."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        return next((data[k] for k in keys if isinstance(data.get(k), str)), None)
    return data if isinstance(data, str) else raw


def _first_absolute_path(raw: Any) -> str:
    """The last absolute path printed in a terminal result (``pwd``)."""
    text = _unwrap(raw, ("output", "stdout", "result", "content", "text")) or ""
    return next(
        (
            ln.strip()
            for ln in reversed(text.splitlines())
            if ln.strip().startswith("/") and " " not in ln.strip()
        ),
        "",
    )


def _task_cwd(task_id: str) -> Path | None:
    """The working directory of the backend task that made the tool call, if reachable.

    Asked once per task through the terminal tool, in that task, so a Docker or SSH
    session resolves to the directory the file tools actually used rather than the
    directory this process was launched from.
    """
    tid = task_id or ""
    if not tid:
        return None
    cached = _task_cwds.get(tid)
    if cached is not None:
        return Path(cached) if cached else None
    dispatch = getattr(_ctx, "dispatch_tool", None)
    if not callable(dispatch):
        return None
    try:
        raw = dispatch("terminal", {"command": "pwd"}, task_id=tid)
    except Exception:
        logger.debug("house-style: task cwd probe failed", exc_info=True)
        return None
    first = _first_absolute_path(raw)
    if not first:
        logger.debug("house-style: task cwd probe returned no path (%r)", str(raw)[:120])
        _task_cwds[tid] = ""
        return None
    if len(_task_cwds) > 200:
        _task_cwds.clear()
    _task_cwds[tid] = first
    return Path(first)


def _is_numbered(line: str) -> bool:
    head, sep, _ = line.partition("|")
    return bool(sep) and head.strip().isdigit()


def _strip_line_numbers(raw: Any) -> str | None:
    """Turn ``read_file``'s ``LINE|CONTENT`` listing back into plain text.

    Anything that is not a paginated listing (an error, a refusal, an empty reply) is None: no
    file text was read, and capture must say so rather than store an error as if it were the file.
    """
    text = _unwrap(raw, ("content", "output", "result", "text"))
    lines = (text or "").splitlines()
    numbered = [ln for ln in lines if _is_numbered(ln)]
    if not numbered or len(numbered) < max(1, int(len(lines) * 0.8)):
        return None
    out = [ln.partition("|")[2] if _is_numbered(ln) else ln for ln in lines]
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _engine_for(base_dir: Path) -> Engine:
    """One engine per plugin-data directory and redactor (a profile switch rebuilds it)."""
    global _engine, _redact_source
    with _engine_lock:
        source = getattr(_ctx, "redact_text", None) if _ctx is not None else None
        if _engine is None or _engine.store.base != base_dir or _redact_source is not source:
            _engine = Engine(
                base_dir,
                redact=_host_redact(),
                read_remote=_read_remote,
                remote_note=lambda: _last_read_error,
            )
            _redact_source = source
        return _engine


def engine() -> Engine:
    """Engine for the active profile; raises when the host gives no data dir."""
    return _engine_for(_plugin_data_dir())


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _resolve_tool_path(raw: str, session_id: str = "", task_id: str = "") -> Path:
    """A tool's ``path`` argument, resolved the way the tool itself resolved it.

    The session directory is tried first because it is free; when the file is not there
    — a remote backend, whose files live in the task's own environment — the task's
    working directory is asked for once and used instead.
    """
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    direct = _session_cwd(session_id) / candidate
    try:
        if direct.exists():
            return direct
    except OSError:
        return direct
    task = _task_cwd(task_id)
    return (task / candidate) if task is not None else direct


def _repo_root(path: Path, session_id: str = "", task_id: str = "") -> Path | None:
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
    cwd = _task_cwd(task_id) or _session_cwd(session_id)
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
        path = _resolve_tool_path(raw_path, session_id, task_id)
        root = _repo_root(path, session_id, task_id)
        logger.info(
            "house-style: capture %s raw=%s path=%s root=%s task=%s",
            tool,
            raw_path,
            path,
            root,
            task_id or "-",
        )
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


def _maybe_scan(force: bool = False, session_id: str = "") -> None:
    """Classify stored blocks for the current repository, at most every 30 s."""
    cwd = _session_cwd(session_id)
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
        _remember_cwd(session_id)
        _maybe_scan(force=True, session_id=session_id)
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


def _learn(root: Path, eng: Engine, confirm: bool, cwd: Path) -> str:
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


def _reset(root: Path, eng: Engine, confirmed: bool, cwd: Path) -> str:
    signals, kept, state = eng.reset(root, confirmed=confirmed, cwd=cwd)
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


def _command_root(session_id: str = "") -> Path | None:
    """Repository root for command output, or the session's logical root remotely.

    A Docker or SSH session's working directory does not exist on this host, so the
    usual ``.git`` walk cannot find anything; the session directory itself is then the
    root, which is what capture keys those signals by too.
    """
    cwd = _session_cwd(session_id)
    root = find_repo_root(cwd)
    if root is not None:
        return root
    try:
        visible = cwd.exists()
    except OSError:
        visible = False
    return None if visible else cwd


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

    session_id = str(kwargs.get("session_id") or "")
    if session_id:
        _remember_cwd(session_id)
    cwd = _session_cwd(session_id)
    root = _command_root(session_id)
    if root is None:
        return (
            "house-style: this working directory is not inside a git repository, so there "
            "is nothing to track."
        )
    try:
        eng = engine()
    except Exception as exc:
        return f"house-style: plugin data directory unavailable ({exc})."

    def scan() -> str:
        result = eng.scan(root)
        return (
            f"Scanned {len(result.signals)} recorded edit(s) in {root.name}: {_format_counts(result.counts)}."
        )

    def set_off(off: bool) -> str:
        eng.store.set_off(eng.store.register_repo(root), off)
        return f"house-style is {'off' if off else 'on'} for {root.name}" + (
            ": new edits are not recorded." if off else "."
        )

    actions = {
        "status": lambda: eng.status(root, cwd=cwd, capture_note=NO_REDACTOR),
        "scan": scan,
        "learn": lambda: _learn(root, eng, confirm=False, cwd=cwd),
        "learn-confirm": lambda: _learn(root, eng, confirm=True, cwd=cwd),
        "reset": lambda: _reset(root, eng, confirmed=False, cwd=cwd),
        "reset-confirm": lambda: _reset(root, eng, confirmed=True, cwd=cwd),
        "off": lambda: set_off(True),
        "on": lambda: set_off(False),
        "export": lambda: eng.export(root, cwd),
    }
    if sub not in actions:
        return "Unknown subcommand.\n\n" + _USAGE
    if sub in ("scan", "learn") and not eng.capture_enabled:
        return f"house-style: nothing was {'scanned' if sub == 'scan' else 'learned'} — {NO_REDACTOR}"
    try:
        return actions[sub]()
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
