#!/usr/bin/env python3
"""Bump the PEP 440 beta segment of the package version.

Reads ``__version__`` from ``src/aqua/__init__.py`` (or the path given as the
first CLI argument), increments the beta segment (``X.Y.ZbN`` -> ``X.Y.Zb(N+1)``;
if the current version has no beta segment it becomes ``X.Y.Zb1``), writes the
file back in place, and prints the new version to stdout.

Used by CI to give every merge to ``develop`` its own beta release on TestPyPI.
Only touches the beta counter -- advancing the base release (e.g. 0.5.1 -> 0.6.0)
stays a deliberate manual edit.

``uv version`` cannot be used here: the project declares ``dynamic = ["version"]``
sourced from ``__init__.py`` and uv refuses to get/set dynamic versions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_INIT = Path(__file__).resolve().parent.parent / "src" / "aqua" / "__init__.py"

# Matches the assignment line, capturing the version string in group "ver".
ASSIGN_RE = re.compile(r"""__version__\s*=\s*["'](?P<ver>[^"']+)["']""")
# Matches an X.Y.Z base with an optional PEP 440 beta segment (bN / betaN).
VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?:b(?P<beta>\d+))?$")


def bump(version: str) -> str:
    """Return the next beta version for ``version`` (``X.Y.ZbN``)."""
    match = VERSION_RE.match(version)
    if not match:
        raise ValueError(
            f"version {version!r} is not of the form X.Y.Z or X.Y.ZbN; "
            "advance the base version by hand before auto-bumping betas"
        )
    base = match.group("base")
    next_beta = int(match.group("beta")) + 1 if match.group("beta") else 1
    return f"{base}b{next_beta}"


def main(argv: list[str]) -> int:
    init_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_INIT
    text = init_path.read_text()

    assign = ASSIGN_RE.search(text)
    if not assign:
        print(f"could not find __version__ assignment in {init_path}", file=sys.stderr)
        return 1

    try:
        new_version = bump(assign.group("ver"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    new_text = text[: assign.start()] + f'__version__ = "{new_version}"' + text[assign.end() :]
    init_path.write_text(new_text)
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
