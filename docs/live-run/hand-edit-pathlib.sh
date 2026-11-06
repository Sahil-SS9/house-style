#!/usr/bin/env bash
# Scripted "user correction": rewrite the agent's os.path version as pathlib.
# Repeatable, so the live scenario does not depend on hand-typing.
set -eu
target="${1:?usage: hand-edit-pathlib.sh <file>}"
python3 - "$target" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace("import os", "from pathlib import Path")
for name in ("config.json", "settings.json"):
    for quote in ("'", '"'):
        text = text.replace(
            f"os.path.join(root, {quote}{name}{quote})", f"Path(root) / {quote}{name}{quote}"
        )
path.write_text(text, encoding="utf-8")
print(text)
PY
