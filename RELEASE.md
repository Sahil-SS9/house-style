# Release checklist

Work through this in order. Nothing here publishes anything: the repository, the
catalog entry and the release itself are the owner's calls.

## 1. Local gates (must all pass on the commit you intend to publish)

```bash
uv venv && uv pip install -e ".[dev]"
HERMES_HOST_SRC=/path/to/hermes-agent ./scripts/check.sh   # tests, lint, format, catalog entry
```

- [ ] Core suite green, plugin suite green.
- [ ] No personal paths, no internal hostnames, no names of private tooling anywhere in
      the tree, the tests or the commit messages.
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
- [ ] Every warning is understood and written down. This plugin has three *private core
      imports* warnings (`agent.redact.redact_sensitive_text`,
      `agent.file_safety.is_write_denied`, `hermes_cli.plugins.VALID_HOOKS`). Each is guarded
      so a rename degrades instead of crashing, and each is there because the public
      surface does not expose it (see "Open questions" in the lane report). Do not
      silence them by deleting the guard.
- [ ] `python -c "from hermes_cli.plugin_dev import doctor_plugin; ..."` reports the three
      declared hooks registered and zero findings.

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
- [ ] Classic CLI: `/house-style status` prints counts, not an error.
- [ ] TUI (`hermes --tui`): the same command, same wording.
- [ ] Gateway (Discord or Telegram): the same command, same wording. A surface that
      cannot run it must refuse with a message, not fail silently.
- [ ] The full live scenario from the lane: agent writes a module with `os.path`,
      the user rewrites it to `pathlib` twice across two sessions, `/house-style status`
      shows the edited pairs, `/house-style learn` shows a diff, `/house-style learn
      confirm` writes the file, and a fresh session follows the preference.
- [ ] Live setup untouched: `plugins/` listing unchanged, no `plugins.enabled` write, no
      config edit in the operator's own home.

## 4. Publish (owner only)

- [ ] Create the public repository, push `main`.
- [ ] Tag `v0.1.0`.
- [ ] Read the tag back: `git ls-remote origin refs/heads/main refs/tags/v0.1.0`.
- [ ] Fill `sha:` in `plugin-catalog/house-style.yaml` with the full 40-hex commit.
- [ ] Open the catalog pull request against `NousResearch/hermes-agent` with that entry
      and the disclosure paragraph unchanged.

## 5. After a later change

- [ ] Bump `version` in `plugin.yaml` and `pyproject.toml` together.
- [ ] Re-run section 1 and 2 at the new commit.
- [ ] Send a separate catalog pull request bumping `sha:` (the entry pins a commit, not a
      branch).
