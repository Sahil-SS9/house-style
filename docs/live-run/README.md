# Live run recipe

The scenario the release checklist asks for, written down so it can be repeated against
a throwaway Hermes home without touching anyone's real one. It needs a Hermes build that
exposes `ctx.redact_text()` (see the README's Requirements).

```bash
# A disposable home and a disposable project.
export HOME=/tmp/hs-user-home
export HERMES_HOME=/tmp/hs-hermes
mkdir -p "$HERMES_HOME/plugins" /tmp/hs-project/pkg
cp -r <this checkout> "$HERMES_HOME/plugins/house-style"
printf 'plugins:\n  enabled:\n    - house-style\n' >> "$HERMES_HOME/config.yaml"

cd /tmp/hs-project
git init -q -b main
git config user.email demo@example.invalid && git config user.name "Demo"
git commit -q --allow-empty -m initial
hermes
```

Then, in the session:

1. `Use write_file to create pkg/fsutil.py with one function: config_path(root)
   returning os.path.join(root, 'config.json'). Keep it short.`
2. Leave the session, run `docs/live-run/hand-edit-pathlib.sh pkg/fsutil.py`, and start a
   new session.
3. Repeat step 1 for a second module (`pkg/paths.py`, `settings.json`), edit it the same
   way, and start another new session.
4. `/house-style status` — capture state, the context file, the two edited pairs.
5. `/house-style learn` — the diff for `AGENTS.md`; nothing written yet.
6. `/house-style learn confirm` — the section appears.
7. Add a line of your own inside the generated block, then `/house-style learn` and
   `/house-style learn confirm` again: your line survives and the counts update.
8. Start a new session and ask for a third module. The agent should follow the rules, and
   the rules now sit in the system prompt through Hermes's normal context loading — look
   for the project-context section in the session's stored system prompt, not in a
   per-turn injection.
9. Repeat in a repository that already has `.hermes.md`: the block goes there and no
   `AGENTS.md` is created.
10. Multi-file: one V4A `patch` (`mode="patch"`) touching two files, then one
    `replace_all` edit. `status` counts one signal per hunk and per occurrence.
11. Remote backend: with a Docker terminal environment, edit a file inside the container.
    `status` shows the signal read `tool-surface`, or names the backend under "could not
    read".
12. Gateway: send `/house-style status` through whatever platform the disposable home is
    running (the API server is enough) and check the reply.
