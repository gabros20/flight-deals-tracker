"""
Project-root resolution, independent of the current working directory.

Every data path in the project (config, cache, registry data, history CSVs,
ground-transfer precompute) must be resolved through this module instead of
being built from a cwd-relative string like ``Path("data/...")``. Without
this, running the CLI (or the test suite) from anywhere other than the repo
root silently loads/writes the wrong files (or none at all) — a bug the audit
called out explicitly (`docs/UPGRADE-PLAN.md` §1a: "Cron from any cwd").

Resolution order:
1. ``FLIGHT_DEALS_HOME`` env var, if set — always wins, explicit override.
2. Walk up from this file's location looking for ``pyproject.toml`` (works
   for an editable install: this file lives at ``<root>/src/flight_deals/paths.py``).
3. Best-effort fallback: three parents up from this file (src/flight_deals/paths.py
   -> src/flight_deals -> src -> <root>), in case pyproject.toml is missing
   for some reason (e.g. a packaged, non-editable install).
"""

import os
from pathlib import Path


def get_project_root() -> Path:
    """Return the project root directory, resolved without relying on cwd."""
    env_home = os.environ.get("FLIGHT_DEALS_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent

    # Fallback: src/flight_deals/paths.py -> src/flight_deals -> src -> root
    return here.parents[2]


def resolve_path(path_str: str) -> Path:
    """
    Resolve a data-file path anchored to the project root.

    Absolute paths (or ones with ``~``) are returned as-is (expanded);
    relative paths are anchored to :func:`get_project_root` rather than cwd.
    """
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p
    return get_project_root() / p
