from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_tag_version", SCRIPTS_DIR / "check_tag_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_tag_version = _load_script()

PYPROJECT = """
[project]
name = "storepulse"
version = "0.1.0"
"""


def test_matching_tag_passes() -> None:
    assert check_tag_version.check("v0.1.0", PYPROJECT) is None


def test_mismatched_tag_fails() -> None:
    error = check_tag_version.check("v0.2.0", PYPROJECT)
    assert error is not None
    assert "v0.2.0" in error and "0.1.0" in error


def test_malformed_tag_fails() -> None:
    error = check_tag_version.check("release-1", PYPROJECT)
    assert error is not None
    assert "doesn't look like a version tag" in error


def test_prerelease_tag_matches_prerelease_version() -> None:
    pre = '[project]\nname = "storepulse"\nversion = "0.2.0-rc1"\n'
    assert check_tag_version.check("v0.2.0-rc1", pre) is None


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT)
    monkeypatch.setattr(check_tag_version, "PYPROJECT_PATH", pyproject)
    assert check_tag_version.main(["v0.1.0"]) == 0
    assert check_tag_version.main(["v9.9.9"]) == 1
    assert check_tag_version.main([]) == 2
    assert check_tag_version.main(["v0.1.0", "extra"]) == 2


def test_actual_pyproject_matches_current_version() -> None:
    # Guards against the version being bumped in one place and not the other.
    root = Path(__file__).resolve().parents[1]
    from storepulse import __version__

    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert check_tag_version.project_version(text) == __version__


# -- workflow sanity: a light text check, no YAML parser (no new dependency) ----------------
#
# This intentionally doesn't parse the YAML structurally: it just guards against someone
# editing the workflow and forgetting to keep the tag-check script wired in, or loosening
# the trusted-publishing permissions.


def test_release_workflow_checks_tag_with_the_script() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in text
    assert "scripts/check_tag_version.py" in text
    assert "python -m build" in text
    assert "id-token: write" in text
    assert "environment: pypi" in text
    assert "pypa/gh-action-pypi-publish@" in text
