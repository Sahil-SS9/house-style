# house-style

Learns a project's house style from how you correct the agent's edits, then writes it
down as preferences you own.

Every time the agent writes a file, house-style records the block it introduced. Later
it looks at what you did with that block: left it alone, rewrote it, or took it out
again. `/house-style learn` sends a bounded batch of those before/after pairs to your
model, which turns them into a short numbered list such as:

```
1. Prefer pathlib over os.path (4 edits)
2. Return early instead of nesting conditionals (2 edits)
3. Keep user-facing strings in en-GB spelling (2 edits)
```

That list lives in `.hermes/house-style.md` in your repository. You can read it, edit
it, delete lines from it, or delete the file. Re-learning merges into it and never
drops a line you wrote. On the first turn of a session in that repository, the list is
added to the message sent to the model as ephemeral context, so the system prompt is
untouched and the prompt cache stays byte-stable.

## Where it came from

The idea, and the name "taste", come from Command Code's taste feature: accept, reject
and post-accept edits are signals, a model distils them into a per-project file, and
that file can be shared. house-style takes the same first three steps and stops.

Differences, stated plainly:

- **No packages.** Taste files can be pushed, pulled and linted between people.
  house-style has no push, pull, registry or team server. `/house-style export` prints
  the markdown so you can share it by hand if you want to.
- **No other-agent history.** There is no `/learn-taste` equivalent that mines your
  past sessions in Claude Code or Codex. Adapters for those harnesses are possible (see
  below) but are not built.
- **The core is harness-agnostic.** Capture, classification, the preferences file and
  the distillation prompt are a plain Python package (`house_style_core`) with no host
  imports. The Hermes plugin is a thin adapter over it.
- **Hermes only, for now.** Injection uses Hermes's `pre_llm_call` hook. Nothing here
  reads another agent's transcripts.

## Install

From a checkout, with Hermes' plugin directory on your `HERMES_HOME`:

```bash
cp -r house-style "$HERMES_HOME/plugins/house-style"
hermes plugins enable house-style
```

Or, once the repository is public:

```bash
hermes plugins install Sahil-SS9/house-style --enable
```

The plugin needs no Python dependencies and no API keys: it uses the model you already
configured.

## Commands

| Command | What it does |
| --- | --- |
| `/house-style status` | Signal counts for this repository and the newest edited pairs, redacted. |
| `/house-style scan` | Re-check what happened to the edits it recorded. Also runs on session start and once per turn (at most every 30 seconds). |
| `/house-style learn` | Distil the batch with your model and show the diff of `.hermes/house-style.md`. Nothing is written. |
| `/house-style learn confirm` | Write the staged diff, keeping any line you added or rewrote in the meantime. |
| `/house-style reset` | Show what would be cleared: stored signals, the pending proposal, and the learned entries. |
| `/house-style reset confirm` | Clear them. Lines you wrote in the file survive. |
| `/house-style off` / `on` | Stop or resume recording and injecting for this repository. |
| `/house-style export` | Print the preferences file. |

All of these are plugin slash commands, so they work in `hermes`, in `hermes --tui`
and on the messaging gateway from the same registration.

## Settings

One key, read from the plugin's own settings block:

```yaml
plugins:
  entries:
    house-style:
      settings:
        inject: first_turn    # first_turn (default) or off
```

`off` keeps recording and learning but never adds anything to a turn. Any other value
is reported in `/house-style status` and treated as `off`, rather than being silently
ignored.

## What is stored, and where

Under `plugin-data/house-style/`, one file per repository:

- `signals/<key>.jsonl` — the captured blocks: file path relative to the repository
  root, a hash of the file as written, the block of lines the agent introduced, up to
  three lines of context either side, and for a `patch` the text it replaced.
- `signals/<key>.repo.json` — the repository path, the `off` flag, and the entries that
  were generated last time.
- `pending/<key>.json` — a staged learn proposal, until you confirm or ignore it.

Each repository keeps at most 400 rows and 512 KB, oldest dropped first. At most 50
repositories are tracked; files untouched for 90 days are removed on the next sweep.

Files are skipped, and the reason reported in debug logs, when they are outside a git
repository, are binary, are larger than 256 KB, have a secret-like name (`.env`,
`*.pem`, `id_rsa*`, `credentials*` and similar), or are on Hermes' own protected-path
list (`agent/file_safety.py`). Every stored line passes through Hermes'
`redact_sensitive_text(..., force=True)` first, and the same redaction is applied to
the file when it is compared later, so a secret you left untouched cannot read as an
edit.

## Disclosure

- **Hooks used:** `post_tool_call` (for `write_file` and `patch` only),
  `on_session_start` and `pre_llm_call`.
- **What it reads:** the arguments those tools were given, and the working-tree files
  they just wrote. Nothing else in your filesystem.
- **What it stores:** redacted before/after pairs in Hermes' plugin-data directory.
- **What it writes:** `.hermes/house-style.md` in the project, and only after
  `/house-style learn confirm`.
- **Model calls:** one per `/house-style learn`.
- **Network:** none beyond that model call. No telemetry, no subprocesses.

## What an adapter for another harness needs from the core

The core takes five inputs, and nothing else. An adapter that can supply them can
support house-style in Claude Code, Codex or OpenCode without touching the core:

1. **A post-edit event** carrying the tool name and arguments (`path` plus the text
   written). In Claude Code that is a `PostToolUse` hook filtered to `Write` and
   `Edit`; in Codex and OpenCode it is their own tool-callback seam. The core wants the
   *block introduced*, not the whole file: for a replace-style edit, pass the new text
   as `written_text` and the matched old text as `pre_text`.
2. **A resolved absolute path and the repository root**, because the core stores paths
   relative to the root and keys its store by root. If the harness runs tools in a
   container or over SSH, the file will not be readable on the host and capture is
   skipped; the adapter should say so rather than pretend.
3. **A redactor**: any `fn(text) -> text`. Pass the harness's own secret scrubber; the
   core's regex fallback is a floor, not a substitute.
4. **A protected-path predicate**: `fn(path) -> bool`. Reuse the harness's deny list so
   the two cannot drift.
5. **A model call**: `messages -> text`, plus a way to read usage if you want to
   report spend. Batching (`select_pairs`, `build_messages`) and parsing
   (`parse_response`) are core functions, so only the transport is harness-specific.

Two harness-level behaviours are worth copying rather than reimplementing:

- inject on the **first turn of a session only**, appended to the user message, never
  to the system prompt;
- run the classification pass on a **cheap trigger** (session start and once per turn,
  throttled) rather than on every tool call, because classification reads files.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check .
```

The core tests use real temporary git repositories. The plugin tests install a fake
host (plugin-data directory, redactor, protected-path list, tool-path resolver) so they
run without a Hermes checkout; the live run in the release checklist is what proves it
inside a real Hermes.

## Licence

MIT.
