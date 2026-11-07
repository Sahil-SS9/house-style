# Design notes: what is captured, and how it is judged

The reference for reviewing the plugin or writing an adapter. Code:
`src/house_style_core`.

## The signal

One row per introduced block, appended to
`plugin-data/house-style/signals/<key>.jsonl`:

| field | meaning |
| --- | --- |
| `id`, `ts` | opaque id, UTC timestamp |
| `tool` | `write_file` or `patch` |
| `path` | path relative to the repository root, forward slashes (for a remote backend: the path as the tool saw it) |
| `content_sha256` | SHA-256 of the file exactly as the tool left it |
| `hunk` | the block the tool introduced, redacted |
| `before_ctx` / `after_ctx` | up to three lines either side, redacted, used as fences |
| `pre_hunk` | for a replace patch, the text it replaced |
| `read_via` | `host-disk` or `tool-surface` |
| `task_id` | the backend task that made the edit, used to read the file back for comparison |
| `occurrence` / `occurrences` | which replaced copy this is, and how many there were |
| `start_line`, `total_lines`, `size` | position and size at capture time |

The `hunk` is the block, not the file, for a reason: a patch that changes one line
should not re-send a 400-line file, and the class of edit we care about ("did the user
accept this block?") is local.

## Remote backends

A file the host cannot see is read through `ctx.dispatch_tool("read_file", …,
task_id=<the hook's task id>)`: the registry hands `task_id` to the read handler, which
resolves relative paths against that task's working directory and reads through its
environment. The repository root for such an edit is that task's directory too (asked
for once per task via the terminal tool), so capture and the commands agree. The same
task-scoped read is used when comparing the file later, so a Docker edit is judged
rather than reported as reverted because the host has no such file.

## Landing check

A write is only recorded if the file on disk matches what the tool was asked to write
(`write_file`: content hash; `patch`: the introduced block is present). A write denied
by the host's own guards leaves the file untouched, so it produces no signal — which is
why house-style needs no private protected-path list.

## Locating the block again

Line numbers are useless once anything above the block moves, so `locate()` tries three
passes:

1. **exact**: a verbatim occurrence anywhere in the file. With `occurrences > 1`
   (replace_all) the recorded ordinal decides which copy a signal refers to.
2. **anchored**: the nearest non-blank line from `before_ctx` and from `after_ctx` are
   searched for, and the lines between them are the region. Blank context lines are
   skipped outwards, with the gap added back, because a blank line matches everywhere.
3. **window**: the best-scoring run of lines of the same length, anchored on the most
   distinctive lines of the block.

## Judging it

| outcome | rule |
| --- | --- |
| kept | the located region is identical to `hunk` (trailing whitespace ignored) |
| reverted | the region equals `pre_hunk` ("restored to the pre-edit text"), is empty, the file is gone, or no match scores above 0.5 |
| edited | anything else, recorded as a minimal before/after pair |

Similarity is scored on whole lines first, then on characters when the line score is
low: a one-line hunk the user reworded scores zero on lines even when it is plainly the
same statement. A "kept" verdict only ever comes from an exact comparison — a ratio can
say "edited", never "kept".

## The context file

`contextfile.resolve_target()` mirrors the host's own order (`agent/prompt_builder.py`):
nearest `.hermes.md`/`HERMES.md` → `AGENTS.override.md`/`AGENTS.md`/`agents.md` from the
git root down → `CLAUDE.md` → `.cursorrules` / `.cursor/rules/*.mdc` → a new
`AGENTS.md`. Only the region between the two HTML comments is ever rewritten, and a
re-learn compares that region against what it generated last time, so every difference
is treated as the user's and preserved. Emptying the learned set removes the whole
section.

## Budgets

- files: 256 KiB, 200 lines per block, 12,000 characters per block
- store: 400 rows and 512 KB per repository, 50 repositories, 90-day expiry
- replace_all: at most 50 occurrences per call
- distillation: 24 pairs, 6,000 characters, 900 characters per side, 12 rules out

## Redaction

The host's `ctx.redact_text()` runs first, at its strongest setting, honouring
`security.redact_secrets`. The core then applies its own narrow floor (known token
prefixes, private-key blocks, credential-shaped assignments, URL userinfo) so a host
pass that misses a shape cannot put raw text on disk.

`ctx.redact_text()` raises on internal failure. That propagates: the edit is not stored
at all, and an edit whose file cannot be scrubbed for comparison is left unjudged and
reported as such, never silently labelled kept or reverted.

## What it deliberately does not do

- No capture outside a git repository, so there is always a root to be relative to.
- No capture of secret-like filenames, binary files, oversized files, or `.git/` internals.
- No writes to the context file without `/house-style learn confirm`.
- No reading of other agents' transcripts, no team packages, no push or pull.
- No V4A deletions or moves (nothing agent-written is left to judge).
