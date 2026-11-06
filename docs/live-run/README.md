# Live run recipe

The live scenario the release checklist asks for, written down so it can be repeated
against a throwaway Hermes home without touching anyone's real one.

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
4. `/house-style status` — the two edited pairs, redacted.
5. `/house-style learn` — the diff of `.hermes/house-style.md`; nothing written yet.
6. `/house-style learn confirm` — the file appears.
7. Add a line of your own inside the generated block, then `/house-style learn` and
   `/house-style learn confirm` again: your line survives and the counts update.
8. Start a new session and ask for a third module. The agent should follow the
   preferences; the injected block is visible in the session store's `api_content`
   column (not in `content`, which is what keeps the prompt cache byte-stable).
