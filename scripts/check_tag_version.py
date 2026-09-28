#!/usr/bin/env python3
"""Checks that a release tag (e.g. ``v0.1.0``) matches ``pyproject.toml``'s version.

Used by ``.github/workflows/release.yml`` before building or publishing anything;
kept as a standalone script (not inline workflow bash) so it has a unit test.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:[.\-][A-Za-z0-9.]+)?)$")

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


def project_version(pyproject_text: str) -> str:
    data = tomllib.loads(pyproject_text)
    version = data["project"]["version"]
    return str(version)


def version_from_tag(tag: str) -> str | None:
    match = TAG_RE.match(tag)
    return match.group(1) if match else None


def check(tag: str, pyproject_text: str) -> str | None:
    """Returns None if ``tag`` matches the project version, else an error message."""
    version = version_from_tag(tag)
    if version is None:
        return f"tag {tag!r} doesn't look like a version tag (expected v X.Y.Z)"
    expected = project_version(pyproject_text)
    if version != expected:
        return f"tag {tag!r} is version {version!r}, but pyproject.toml has {expected!r}"
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_tag_version.py <tag>", file=sys.stderr)
        return 2
    error = check(args[0], PYPROJECT_PATH.read_text(encoding="utf-8"))
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"tag {args[0]!r} matches pyproject.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
