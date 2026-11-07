# house-style

Learns a project's house style from how you correct the agent's edits, and writes it
into the project context file Hermes already reads — so the rules reach the model with
no injection of their own.

Every time the agent writes a file, house-style records the block it introduced. Later
it looks at what you did with that block: left it alone, rewrote it, or took it out
again. `/house-style learn` sends a bounded batch of those before/after pairs to your
model, which turns them into a short numbered list, and `/house-style learn confirm`
writes that list into `AGENTS.md` (or the context file the project already uses):

```markdown
## House style

<!-- house-style:generated:start -->
1. Prefer pathlib over os.path (4 edits)
2. Return early instead of nesting conditionals (2 edits)
3. Keep user-facing strings in en-GB spelling (2 edits)
<!-- house-style:generated:end -->
```

You own that file. The block is the only region house-style ever rewrites: everything
outside it is preserved, and any line you add or reword inside it is carried across the
next learn. Because it is the file Hermes loads as project context, the rules arrive in
the classic CLI, the TUI and every gateway surface by the normal route, and Claude Code
reads the same `AGENTS.md`.

## Requirements

house-style redacts through the host with `ctx.redact_text()`, a plugin seam that is not
in mainline Hermes yet (it is open as `feat/plugin-redact-text`, drafted for the share
plugin too). On a build without it the plugin loads, records nothing, and says so:

```
/house-style status
  capture: OFF — this Hermes build has no ctx.redact_text(), so nothing is recorded.
```

There is no fallback to Hermes internals: the plugin uses only documented `ctx`
methods, and would rather do nothing than reach inside the host. If the host redactor
itself raises, the edit is not stored at all — and an edit whose file cannot be scrubbed
for comparison is left unjudged rather than reported as kept or reverted.

## Install

From a checkout, with the plugin directory under your `HERMES_HOME`:

```bash
cp -r house-style "$HERMES_HOME/plugins/house-style"
hermes plugins enable house-style
```

Or, once the repository is public:

```bash
hermes plugins install Sahil-SS9/house-style --enable
```

No Python dependencies, no API keys: it uses the model you already configured.

## Commands

| Command | What it does |
| --- | --- |
| `/house-style status` | Capture state, the context file it will use, rule and signal counts, how files were read, and the newest edited pairs (redacted). |
| `/house-style scan` | Re-check what happened to the edits it recorded. Also runs at each new session. |
| `/house-style learn` | Distil the batch with your model and show the diff. Nothing is written. |
| `/house-style learn confirm` | Write the staged rules, keeping any line you added or reworded in the meantime. |
| `/house-style reset` | Show what would be cleared: stored signals, pending proposal, learned rules. |
| `/house-style reset confirm` | Clear them. Lines you wrote in the file survive. |
| `/house-style off` / `on` | Stop or resume recording for this project. |
| `/house-style export` | Print the rules as markdown. |

All of these are plugin slash commands, so they work in `hermes`, in `hermes --tui` and
on the messaging gateway from the same registration.

There are no settings to configure.

## What is captured

`post_tool_call`, for `write_file` and `patch` only:

- **`write_file`**: the whole file, verified against the content the tool was asked to
  write. If the file on disk is not what the tool wrote — a denied or failed write —
  nothing is recorded.
- **`patch`, replace mode**: the replacement block, and the text it replaced. With
  `replace_all` every replaced occurrence is captured, one signal each, and each is
  judged separately.
- **`patch`, V4A mode** (`*** Begin Patch` …): every hunk in every file, including added
  files. Deletions and moves are not captured (there is no agent-written code left to
  judge) and are listed as skipped in debug logs.

The file is read back after the write, so the stored hash and context describe what is
actually there. On a remote backend (Docker, SSH, Modal) the host cannot see the file,
so it is read through the `read_file` tool instead, and `status` shows how many signals
came from each route. A backend that cannot be read at all is named in `status` with the
session it belongs to — never a silent gap.

The read runs in the task that made the edit (`ctx.dispatch_tool(..., task_id=...)`
reaches the handler's task-aware read), and the repository root for such an edit comes
from that task's own working directory, asked for once per task. When a backend still
cannot be read — a task whose environment is gone, or one that refuses the read — the
edit is recorded as "could not read on backend …" with the host's own answer in
`status`, never a silent gap.

Skipped, with the reason in debug logs: files outside a git repository, binary files,
files over 256 KB, secret-like names (`.env`, `*.pem`, `id_rsa*`, `credentials*` and
similar), and anything inside `.git/` or other tooling directories.

Every stored line goes through the host's redactor first, and the same redaction is
applied to the file when it is compared later — so a secret you left untouched cannot
read as an edit.

## Where the rules go

The first file Hermes would load for the project wins:

1. `.hermes.md` or `HERMES.md` (nearest, walking up to the repository root)
2. `AGENTS.md` (or `AGENTS.override.md`, `agents.md`; root first)
3. `CLAUDE.md`
4. `.cursorrules`, or the first `.cursor/rules/*.mdc`
5. none of the above: a new `AGENTS.md` at the repository root

So house-style never shadows a context file that is already in use, and never creates a
second source of project instructions.

## Where it keeps its own data

Under Hermes' plugin-data directory, one file per repository:

- `signals/<key>.jsonl`: path relative to the repository, a hash of the file as written,
  the block of lines the agent introduced, up to three lines of context either side, and
  for a patch the text it replaced.
- `signals/<key>.repo.json`: the repository path, the `off` flag, the rules generated
  last time, and any files a backend would not let us read back.
- `pending/<key>.json`: a staged learn proposal until you confirm or ignore it.

Each repository keeps at most 400 rows and 512 KB, oldest dropped first. At most 50
repositories are tracked; files untouched for 90 days are removed on the next pass.

## Disclosure

- **Hooks used:** `post_tool_call` (for `write_file` and `patch` only) and
  `on_session_start`.
- **What it reads:** those tools' arguments, and the files they just wrote — from the
  host's filesystem, or through the `read_file` tool when the file lives on a remote
  backend.
- **What it stores:** redacted before/after pairs in Hermes' plugin-data directory.
- **What it writes:** one marked section of the project's context file
  (`.hermes.md`, `AGENTS.md`, `CLAUDE.md` or `.cursorrules`, else a new `AGENTS.md`), and
  only after `/house-style learn confirm`.
- **Model calls:** one per `/house-style learn`.
- **Network:** none beyond that model call. No telemetry, no subprocesses.

## Where it came from

The idea, and the name "taste", come from Command Code's taste feature: accept, reject
and post-accept edits are signals, a model distils them into a per-project file, and
that file can be shared. house-style takes the same first three steps and stops.

- **No packages.** No push, pull, registry or team server. `/house-style export` prints
  the markdown so you can share it by hand.
- **No other-agent history.** No `/learn-taste` equivalent that mines past sessions in
  Claude Code or Codex.
- **No separate file of its own.** The rules go into the project context file Hermes
  already loads, which is also what Claude Code reads, instead of a bespoke file behind
  a hook.
- **The core is harness-agnostic.** Capture, classification, the block merge and the
  distillation prompt are a plain Python package (`house_style_core`) with no host
  imports. The Hermes plugin is a thin adapter over it.

## What an adapter for another harness needs from the core

Five inputs, and nothing else:

1. **A post-edit event** carrying the tool name and arguments (the path plus the text
   written). In Claude Code that is a `PostToolUse` hook filtered to `Write` and `Edit`;
   in Codex and OpenCode it is their own tool-callback seam. The core wants the *block
   introduced*, not the whole file: for a replace-style edit pass the new text as
   `added` and the matched old text as `pre`.
2. **An absolute path and a repository root**, because the core stores paths relative to
   the root and keys its store by root.
3. **A way to read a file the harness process cannot see** (a container or SSH backend),
   as `fn(raw_path) -> text | None`.
4. **A redactor** `fn(text) -> text`: the harness's own scrubber. Without one the core
   refuses to capture rather than storing raw text.
5. **A model call** `messages -> text`, plus usage if you want to report spend. Batching
   and parsing are core functions, so only the transport is harness-specific.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
HERMES_HOST_SRC=/path/to/hermes-agent ./scripts/check.sh   # tests, lint, format, catalog entry
```

The core tests use real temporary git repositories. The plugin tests drive the adapter
against a fake host that implements `ctx` (redaction, session cwd, tool dispatch, model)
and the plugin-data directory, so they run without a Hermes checkout; the live run in
the release checklist is what proves it inside a real Hermes.

## Licence

MIT.
