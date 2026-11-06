#!/usr/bin/env bash
# Every gate this repository has, in one command. Exit 0 only if all of them pass.
#
#   ./scripts/check.sh
#
# HERMES_HOST_SRC=/path/to/hermes-agent adds the host's catalog-entry validator
# (scripts/validate_plugin_catalog.py). Without it that one check reports SKIPPED
# rather than pretending to pass.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
fail=0

run() {
    printf '== %s\n' "$1"
    shift
    if "$@"; then echo "   ok"; else echo "   FAILED"; fail=1; fi
}

run "tests" "$PY" -m pytest tests -q
run "lint" "$PY" -m ruff check .
run "format" "$PY" -m ruff format --check .
run "whitespace" git diff --check

validator="${HERMES_HOST_SRC:-}/scripts/validate_plugin_catalog.py"
host_py="${HERMES_HOST_SRC:-}/.venv/bin/python"
if [ -f "$validator" ] && [ -x "$host_py" ]; then
    # The validator imports ruamel.yaml, which only the host's provisioned venv has.
    run "catalog entry" "$host_py" "$validator" plugin-catalog/house-style.yaml
elif [ -f "$validator" ]; then
    echo "== catalog entry"
    echo "   SKIPPED (no provisioned interpreter at \$HERMES_HOST_SRC/.venv/bin/python)"
else
    echo "== catalog entry"
    echo "   SKIPPED (set HERMES_HOST_SRC to a hermes-agent checkout)"
fi

echo
[ "$fail" -eq 0 ] && echo "all gates passed" || echo "one or more gates failed"
exit "$fail"
