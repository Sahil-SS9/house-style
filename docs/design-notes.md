# Design notes: what is captured, and how it is judged

This is the reference for anyone reviewing the plugin or writing an adapter for
another harness. The code is `src/house_style_core`.

## The signal

One row per agent edit, appended to `plugin-data/house-style/signals/<key>.jsonl`:

| field | meaning |
| --- | --- |
| `id` | opaque 16-hex id |
| `ts` | UTC timestamp, second resolution |
| `tool` | `write_file` or `patch` |
| `path` | path relative to the repository root, forward slashes |
| `content_sha256` | SHA-256 of the file exactly as the tool left it |
| `hunk` | the block of lines the tool introduced (whole file for `write_file`, the replacement text for `patch`), redacted |
| `before_ctx` / `after_ctx` | up to three lines either side, redacted, used as fences |
| `pre_hunk` | for `patch`, the text it replaced |
| `start_line`, `total_lines`, `size` | position and size at capture time |

The `hunk` is the block, not the whole file, for a reason: a `patch` that changes one
line should not re-send a 400-line file to the model, and the class of edit we care
about ("did the user accept this block?") is local.

## Locating the block again

Line numbers are useless once anything above the block moves, so `locate()` tries
three passes:

1. **exact** — a verbatim occurrence anywhere in the file.
2. **anchored** — the nearest non-blank line from `before_ctx` and from `after_ctx` are
   searched for, and the lines between them are the region. Blank context lines are
   skipped outwards, with the gap added back, because a blank line matches everywhere.
3. **window** — the best-scoring run of lines of the same length, anchored on the most
   distinctive lines of the block.

## Judging it

| outcome | rule |
| --- | --- |
| kept | the located region is identical to `hunk` (trailing whitespace ignored) |
| reverted | the region equals `pre_hunk` ("restored to the pre-edit text"), is empty, the file is gone, or no match scores above 0.5 |
| edited | anything else, recorded as a minimal before/after pair |

Similarity is scored on whole lines first, then on characters when the line score is
low: a one-line hunk the user reworded scores zero on lines even when it is plainly
the same statement.

A "kept" verdict is only ever produced by an exact comparison. A ratio can say
"edited", never "kept", so a near-miss cannot masquerade as untouched code.

## Budgets

- files: 256 KiB, 200 lines per block, 12,000 characters per block
- store: 400 rows and 512 KiB per repository, 50 repositories, 90-day expiry
- distillation: 24 pairs, 6,000 characters, 900 characters per side, 12 entries out
- injection: 2,000 characters, first turn of a session only

## Redaction

Every stored string goes through the harness redactor with `force=True`, then the
core's own narrow fallback (known token prefixes, private-key blocks, credential-shaped
assignments, URL userinfo). At classification time the file's current text is redacted
the same way before comparing, so a secret the user left untouched cannot read as an
edit.

## What it deliberately does not do

- No capture outside a git repository, so there is always a root to be relative to.
- No capture of secret-like filenames, binary files, oversized files, or Hermes'
  protected paths.
- No writes to the preferences file without `/house-style learn confirm`.
- No reading of other agents' transcripts, no team packages, no push or pull.
