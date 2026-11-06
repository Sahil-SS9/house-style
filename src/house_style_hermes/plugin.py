"""Hermes adapter: hooks, commands and the host seams house-style needs.

This module is the only place that knows about Hermes. The behaviour lives in
``house_style_core``; here we only answer four questions the core cannot: which
repository the user is working in, what a file tool wrote, who redacts secrets, and
who makes the model call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from house_style_core import distill, prefs
from house_style_core.engine import PREFS_RELPATH, Engine
from house_style_core.gitio import find_repo_root

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

_engine: Engine | None = None
_engine_lock = threading.Lock()
_ctx: Any = None
_last_scan: dict[str, float] = {}
_injected: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# host seams
# ---------------------------------------------------------------------------
def _plugin_data_dir() -> Path:
    """The host's own plugin-data directory. No fallback: a refusal is reported."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _host_redact(text: str) -> str:
    """The host redactor, forced on: a preference file must never hold a credential."""
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True)


def _is_protected_path(path: str) -> bool:
    """The host's own protected-path list (denied writes)."""
    from agent.file_safety import is_write_denied

    return bool(is_write_denied(path))


def _agent_cwd() -> Path:
    """The working directory the tools use, not merely the process cwd."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except Exception:
        return Path(os.getcwd())


def _resolve_tool_path(raw: str, task_id: str) -> Path | None:
    """Resolve a tool's ``path`` argument against the agent's working directory.

    Only the working directory is consulted (absolute paths pass through). Anything
    the host resolved elsewhere — a container or SSH backend — is not readable here
    anyway, and capture reads the file back from disk, so it is skipped rather than
    guessed at.
    """
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    for base in (_agent_cwd(), Path(os.getcwd())):
        try:
            resolved = base / candidate
        except TypeError:
            continue
        if resolved.exists():
            return resolved
    return _agent_cwd() / candidate


def _engine_for(base_dir: Path) -> Engine:
    """One engine per plugin-data directory (a profile switch rebuilds it)."""
    global _engine
    with _engine_lock:
        if _engine is None or _engine.store.base != base_dir:
            _engine = Engine(base_dir, redactor=_host_redact, is_protected=_is_protected_path)
        return _engine


def engine() -> Engine:
    """Engine for the active profile; raises when the host cannot give a data dir."""
    return _engine_for(_plugin_data_dir())


def _inject_mode() -> tuple[str, str]:
    """``(mode, note)`` for the ``inject`` setting; unknown values refuse, never no-op."""
    raw: Any = "first_turn"
    try:
        if _ctx is not None:
            raw = _ctx.get_config("inject", "first_turn")
    except Exception as exc:  # a broken config read must not inject silently
        return "off", f"could not read the inject setting ({exc})"
    value = str(raw if raw is not None else "").strip().lower()
    if value in ("", "first_turn", "first-turn", "firstturn"):
        return "first_turn", ""
    if value == "off":
        return "off", ""
    return "off", (f"unknown inject setting {raw!r}: expected first_turn or off, so nothing is injected")


# ---------------------------------------------------------------------------
# capture and classification
# ---------------------------------------------------------------------------
def _capture(tool_name: str, args: dict, status: str, task_id: str, session_id: str, turn_id: str) -> None:
    if status not in OK_STATUSES:
        return
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return
    if tool_name == "patch" and not isinstance(args.get("new_string"), str):
        return  # V4A multi-file patches are out of scope; no single file to record
    path = _resolve_tool_path(raw_path, task_id)
    if path is None:
        return
    root = find_repo_root(path)
    if root is None:
        return
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return
    if rel == PREFS_RELPATH:
        return  # our own preferences file is not evidence about the user

    written = args.get("content") if tool_name == "write_file" else args.get("new_string")
    if not isinstance(written, str):
        return
    pre_text = args.get("old_string") if tool_name == "patch" else None
    if not isinstance(pre_text, str):
        pre_text = None

    eng = engine()
    key = eng.store.register_repo(root)
    if eng.store.is_off(key):
        return
    capture = eng.capture(
        repo_root=root,
        file_path=path,
        tool=tool_name,
        written_text=written,
        pre_text=pre_text,
        session_id=session_id,
        turn_id=turn_id,
    )
    if not capture.stored:
        logger.debug("house-style: not captured: %s", capture.reason)


def _maybe_scan(force: bool = False) -> None:
    """Classify stored edits for the current repository, at most every 30 s."""
    root = find_repo_root(_agent_cwd())
    if root is None:
        return
    now = time.monotonic()
    last = _last_scan.get(str(root), 0.0)
    if not force and (now - last) < SCAN_MIN_INTERVAL:
        return
    _last_scan[str(root)] = now
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
        _capture(tool_name, args or {}, status or "", task_id or "", session_id or "", turn_id or "")
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


def on_pre_llm_call(
    session_id: str = "",
    is_first_turn: bool = False,
    **kwargs: Any,
) -> dict[str, str] | None:
    """Scan on every turn; inject the preferences on the first turn of a session."""
    try:
        _maybe_scan()
        if not is_first_turn:
            return None
        mode, note = _inject_mode()
        if note:
            logger.debug("house-style: %s", note)
        if mode == "off":
            return None
        root = find_repo_root(_agent_cwd())
        if root is None:
            return None
        token = (session_id or "", str(root))
        if token in _injected:
            return None
        eng = engine()
        if eng.store.is_off(eng.store.register_repo(root)):
            return None
        block = eng.injection(root)
        if not block:
            return None
        _injected.add(token)
        return {"context": block}
    except Exception:
        logger.debug("house-style: injection failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# slash command
# ---------------------------------------------------------------------------
_USAGE = (
    "Usage: /house-style [status|scan|learn|learn confirm|reset [confirm]|off|on|export]\n"
    "  status    signal counts and the newest edited pairs\n"
    "  scan      re-check what happened to the edits it recorded\n"
    "  learn     distil preferences with the model and show the diff\n"
    "  reset     clear signals and learned entries (add confirm to apply)\n"
    "  off / on  stop or resume recording this project\n"
    "  export    print .hermes/house-style.md"
)


def _current_repo() -> Path | None:
    return find_repo_root(_agent_cwd())


def _format_counts(counts: dict[str, int]) -> str:
    return (
        f"{counts.get('kept', 0)} kept, {counts.get('edited', 0)} edited, "
        f"{counts.get('reverted', 0)} reverted"
    )


def _learn(root: Path, eng: Engine, confirm: bool) -> str:
    if confirm:
        rendered = eng.confirm(root)
        if rendered is None:
            return "No proposal is waiting. Run /house-style learn first."
        entries = prefs.read_prefs(eng.prefs_path(root)).entries()
        return (
            f"Wrote {PREFS_RELPATH} ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}). "
            "Review or edit it any time; it is yours."
        )

    messages, pair_count = eng.proposal(root)
    if pair_count == 0:
        return (
            "Nothing to learn from yet. Edit some code the agent wrote, then run "
            "/house-style scan (or just work on: it checks each turn)."
        )
    if getattr(_ctx, "llm", None) is None:
        return "This Hermes build does not expose a model to plugins, so nothing was learned."

    result = _ctx.llm.complete(
        messages=messages,
        max_tokens=LEARN_MAX_TOKENS,
        timeout=LEARN_TIMEOUT,
        purpose="house-style learn",
    )
    learned = prefs.parse_response(getattr(result, "text", "") or "")
    if not learned:
        return "The model returned no usable preferences; nothing was staged."

    before, after = eng.stage(root, learned)
    diff = distill.diff_preview(before, after)
    cost = getattr(getattr(result, "usage", None), "cost_usd", None)
    spent = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""
    model = getattr(result, "model", "") or "the session model"
    return (
        f"Staged {len(learned)} preference(s) from {pair_count} example(s) "
        f"using {model}{spent}.\n\n{diff}\n\n"
        "Run /house-style learn confirm to write it, or ignore this to leave the file alone."
    )


def _reset(root: Path, eng: Engine, confirmed: bool) -> str:
    signals, kept, state = eng.reset(root, confirmed=confirmed)
    if state == "planned":
        return (
            f"Would clear {signals} stored edit(s) and every learned preference"
            + (f", keeping {kept} line(s) you wrote" if kept else "")
            + ".\nRun /house-style reset confirm to do it."
        )
    return (
        f"Cleared {signals} stored edit(s) and the learned preferences"
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
        sub = "learn-confirm"
        rest = ""
    elif sub == "reset" and rest.lower() == "confirm":
        sub = "reset-confirm"
        rest = ""

    try:
        root = _current_repo()
        if root is None:
            return "house-style: this directory is not inside a git repository, so there is nothing to track."
        eng = engine()
    except Exception as exc:
        return f"house-style: plugin data directory unavailable ({exc})."

    try:
        if sub == "status":
            note = _inject_mode()[1]
            body = eng.status(root)
            return body + (f"\n  note: {note}" if note else "")
        if sub == "scan":
            scan = eng.scan(root)
            return (
                f"Scanned {len(scan.signals)} recorded edit(s) in {root.name}: {_format_counts(scan.counts)}."
            )
        if sub == "learn":
            return _learn(root, eng, confirm=False)
        if sub == "learn-confirm":
            return _learn(root, eng, confirm=True)
        if sub == "reset":
            return _reset(root, eng, confirmed=False)
        if sub == "reset-confirm":
            return _reset(root, eng, confirmed=True)
        if sub == "off":
            eng.store.set_off(eng.store.register_repo(root), True)
            return f"house-style is off for {root.name}: new edits are not recorded and nothing is injected."
        if sub == "on":
            eng.store.set_off(eng.store.register_repo(root), False)
            return f"house-style is on for {root.name}."
        if sub == "export":
            return eng.export(root)
        return "Unknown subcommand.\n\n" + _USAGE
    except Exception as exc:  # never raise into the operator's terminal
        logger.debug("house-style: command failed", exc_info=True)
        return f"house-style: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def _hook_supported(name: str) -> bool:
    """Register only hooks this host defines; an unknown one would be dead weight."""
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return name in VALID_HOOKS
    except Exception:
        return True


def register(ctx: Any) -> None:
    """Wire house-style into Hermes. Writes nothing."""
    global _ctx
    _ctx = ctx
    for hook, handler in (
        ("post_tool_call", on_post_tool_call),
        ("on_session_start", on_session_start),
        ("pre_llm_call", on_pre_llm_call),
    ):
        if _hook_supported(hook):
            ctx.register_hook(hook, handler)
    ctx.register_command(
        "house-style",
        handle_command,
        description=(
            "Learn this project's house style from your corrections: "
            "status, scan, learn, reset, off, on, export"
        ),
        args_hint="status|scan|learn|reset|off|on|export",
    )
