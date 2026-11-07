# Release checklist

Work through this in order. Nothing here publishes anything: the repository, the
catalog entry and the release itself are the owner's calls.

## 0. Prerequisite: the redaction seam

house-style records nothing until the target Hermes exposes `ctx.redact_text()`. That
method is not in mainline yet; it is open on `feat/plugin-redact-text` (also drafted for
the share plugin). **Do not submit the catalog entry before it merges.** On a build
without it the plugin loads and refuses, which is the correct behaviour, not a bug. The
method raises on internal failure; house-style treats that as a refusal to store, so a
broken scrubber can never put raw text on disk.

## 1. Local gates (must all pass on the commit you intend to publish)

```bash
uv venv && uv pip install -e ".[dev]"
HERMES_HOST_SRC=/path/to/hermes-agent ./scripts/check.sh   # tests, lint, format, catalog entry
```

- [ ] Core suite green, plugin suite green.
- [ ] No personal paths, no internal hostnames, no names of private tooling anywhere in
      the tree, the tests or the commit messages. Attribution is `Sahil-SS9`.
- [ ] `git status --porcelain` is empty apart from the release commit itself.

## 2. Validate against a clean Hermes, not your own install

`hermes plugins validate` runs against whatever Hermes is installed, so a personalised
checkout gives a false green. Use a clean checkout of the target revision:

```bash
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/host
git -C /tmp/host diff --stat origin/main -- hermes_cli/   # must be empty

git clone <this repo> /tmp/pin && git -C /tmp/pin checkout <sha>

HERMES_HOME=$(mktemp -d) PYTHONPATH=/tmp/host \
  /tmp/host/.venv/bin/python -m hermes_cli.main plugins validate /tmp/pin
```

- [ ] `Validation passed.`
- [ ] Every warning is understood and written down. The only finding expected is the
      missing runtime seam above; there should be **no private core import warnings** at
      all — the adapter imports no Hermes module.
- [ ] `doctor_plugin` reports exactly `post_tool_call` and `on_session_start`
      registered, zero findings.

## 3. Install into a throwaway home and drive it

Never install into the live `HERMES_HOME` to test.

```bash
export HOME=/tmp/hs-user-home
export HERMES_HOME=/tmp/hs-hermes
mkdir -p "$HERMES_HOME/plugins"
cp -r /tmp/pin "$HERMES_HOME/plugins/house-style"
# add `plugins.enabled: [house-style]` to $HERMES_HOME/config.yaml
```

- [ ] `hermes plugins list` shows the plugin, enabled, with no load error.
- [ ] Classic CLI: `/house-style status` prints state, not an error.
- [ ] TUI (`hermes --tui`): the same command, same wording.
- [ ] Gateway (a real dispatch, not a dry run): the same command, same wording.
- [ ] The full live scenario: agent writes a module with `os.path`; the user rewrites it
      to `pathlib` twice across two sessions; `status` shows the edited pairs; `learn`
      shows a diff; `learn confirm` writes the block into `AGENTS.md` (or the project's
      own context file); a fresh session's system prompt carries the rules through
      Hermes's normal context loading.
- [ ] A repo that already has `AGENTS.md` keeps it, with the block added; a repo that
      already has `.hermes.md` gets the block there and no new `AGENTS.md`.
- [ ] Multi-file V4A patch and `replace_all` both produce one signal per hunk/occurrence.
- [ ] A remote backend captures through `read_file` in the task that wrote the file
      (verified with `terminal.backend: docker`: the signal records `read_via:
      tool-surface`, the root is the container's directory, and the verdict is `kept`).
      A backend that cannot be read is named in `status` with the host's own answer.
- [ ] Live setup untouched: `plugins/` listing unchanged, no `plugins.enabled` write, no
      config edit in the operator's own home.

## 4. Publish (owner only)

- [ ] Create the public repository, push `main`.
- [ ] Tag `v0.2.0`.
- [ ] Read the tag back: `git ls-remote origin refs/heads/main refs/tags/v0.2.0`.
- [ ] Fill `sha:` in `plugin-catalog/house-style.yaml` with the full 40-hex commit.
- [ ] Open the catalog pull request with that entry and the disclosure paragraph
      unchanged.

## 5. After a later change

- [ ] Bump `version` in `plugin.yaml` and `pyproject.toml` together.
- [ ] Re-run sections 1 and 2 at the new commit.
- [ ] Send a separate catalog pull request bumping `sha:` (the entry pins a commit, not a
      branch).
