"""Directory-plugin entry point.

Hermes mounts a repo-root ``plugin.yaml`` + ``__init__.py`` as
``hermes_plugins.<slug>`` with the plugin directory *not* on ``sys.path``, so the
sibling packages under ``src/`` are reached by three tiers, narrowest first:

1. relative import (the host's directory-plugin mount),
2. absolute import (an installed wheel, or the repo root on ``sys.path``),
3. this file's own ``src/`` directory added to ``sys.path`` (a bare checkout).

The import stays inside ``register`` on purpose: at module scope a relative import
fails when pytest collects this file as a package, and the host's capability probe
would also pay for it.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register house-style with Hermes."""
    try:  # directory install: the host mounts this file as part of a package
        from .src.house_style_hermes.plugin import register as _register
    except ImportError:
        try:  # installed, or the repo root already on sys.path
            from house_style_hermes.plugin import register as _register
        except ImportError:  # last resort: this file's own src/ directory
            import sys
            from importlib import import_module
            from pathlib import Path

            here = Path(__file__).resolve().parent
            for candidate in (here / "src", here):
                if str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
            _register = import_module("house_style_hermes.plugin").register
    _register(ctx)
