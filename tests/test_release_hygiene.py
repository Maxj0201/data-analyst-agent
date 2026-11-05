"""Guards for repo hygiene: Makefile/CI parity, ignored run outputs, the prompt-bump hook."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "check_prompt_versions", ROOT / "scripts" / "check_prompt_versions.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_make_target_has_a_help_line() -> None:
    makefile = (ROOT / "Makefile").read_text()
    phony = set(re.search(r"^\.PHONY:(.*)$", makefile, re.M).group(1).split())  # type: ignore[union-attr]
    targets = set(re.findall(r"^([a-zA-Z0-9_-]+):", makefile, re.M))
    described = set(re.findall(r"^([a-zA-Z0-9_-]+):.*## \S", makefile, re.M))
    assert targets == phony, f"targets missing from .PHONY: {targets ^ phony}"
    assert targets == described, (
        f"targets without a '## description': {sorted(targets - described)}"
    )


def test_ci_runs_the_makefile_targets() -> None:
    """`make check` is what CI runs only while the workflow delegates to the same targets."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    makefile = (ROOT / "Makefile").read_text()
    for target in ("lint", "coverage", "eval"):
        assert re.search(rf"run: make {target}\b", ci), f"ci.yml must run `make {target}`"
    assert re.search(r"^check:\s+install lint coverage eval\b", makefile, re.M)
    assert "uv sync --locked --all-extras --no-extra embeddings" in ci
    assert "$(UV) sync --locked --all-extras --no-extra embeddings" in makefile


def test_pre_commit_config_has_the_required_hooks() -> None:
    cfg = (ROOT / ".pre-commit-config.yaml").read_text()
    for hook in ("ruff", "ruff-format", "uv-lock-check", "prompt-version-bump"):
        assert f"id: {hook}" in cfg
    assert "scripts/check_prompt_versions.py" in cfg


def test_no_tracked_file_is_git_ignored_and_images_live_in_docs_assets() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-z", "-ci", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    assert out.stdout == "", f"tracked but ignored: {out.stdout.split(chr(0))}"
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    images = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]
    assert all(f.startswith("docs/assets/") for f in images), images
    assert not [f for f in files if f.startswith(("traces/", "runs/", "artifacts/"))]


def test_release_files_exist() -> None:
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert "docs/sandbox.md" in (ROOT / "SECURITY.md").read_text()
    assert (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").is_file()
    assert (ROOT / "docs/history/kickoff").is_dir() and not (ROOT / "docs/kickoff").exists()
    assert "docs/history/kickoff/" in (ROOT / "CONTRIBUTING.md").read_text()


def test_httpx_is_declared_once() -> None:
    assert (ROOT / "pyproject.toml").read_text().count('"httpx') == 1


def test_find_unbumped_flags_only_text_changes_without_a_version_bump() -> None:
    find = _load_hook().find_unbumped
    base = {"a": ("1.0.0", "x1"), "b": ("1.0.0", "y1"), "c": ("1.0.0", "z1"), "gone": ("1", "q")}
    current = {
        "a": ("1.0.0", "x2"),  # text changed, version not bumped -> error
        "b": ("1.1.0", "y2"),  # bumped -> fine
        "c": ("1.0.0", "z1"),  # unchanged -> fine
        "new": ("1.0.0", "n1"),  # added -> fine
    }
    assert find(base, current) == ["a"]
