#!/usr/bin/env python3
"""Fail if any prompt's text changed without a version bump (pre-commit hook).

Compares every registered prompt (``core.prompts.PROMPTS`` and ``eval.scorers.JUDGE_PROMPTS``)
between a base git ref (default ``HEAD``) and the working tree. A prompt whose content
fingerprint differs while its version string is unchanged is an error. Prompts added or removed
are fine. This is stricter than ``tests/test_prompts.py``, which only compares against a pin
that can be edited together with the text.

    uv run python scripts/check_prompt_versions.py            # staged changes vs HEAD
    uv run python scripts/check_prompt_versions.py --base origin/main

The base tree is exported with ``git archive`` and imported in a subprocess, so the old prompt
text is computed by the old code (f-strings, schemas and all), not parsed.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_SNAPSHOT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from analyst_agent.core.prompts import PROMPTS
from analyst_agent.eval.scorers import JUDGE_PROMPTS
import analyst_agent
assert analyst_agent.__file__.startswith(sys.argv[1]), analyst_agent.__file__
print(json.dumps({n: [p.version, p.sha256] for n, p in {**PROMPTS, **JUDGE_PROMPTS}.items()}))
"""


def _snapshot(src_dir: Path) -> dict[str, tuple[str, str]]:
    out = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT, str(src_dir)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tempfile.gettempdir(),  # not the repo root: nothing may import from the cwd
    ).stdout
    return {name: (v, sha) for name, (v, sha) in json.loads(out).items()}


def _base_snapshot(ref: str) -> dict[str, tuple[str, str]] | None:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=REPO,
        capture_output=True,
    )
    if probe.returncode != 0:
        return None  # e.g. the very first commit: nothing to compare against
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref, "src"], cwd=REPO, capture_output=True, check=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="prompt-base-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        return _snapshot(Path(tmp, "src").resolve())


def find_unbumped(
    base: dict[str, tuple[str, str]], current: dict[str, tuple[str, str]]
) -> list[str]:
    """Names of prompts whose text changed but whose version did not."""
    return sorted(
        name
        for name, (version, sha) in current.items()
        if name in base and base[name][1] != sha and base[name][0] == version
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="HEAD", help="git ref to compare against (HEAD)")
    args = parser.parse_args(argv)

    base = _base_snapshot(args.base)
    if base is None:
        print(f"check-prompt-versions: no commit {args.base!r}; nothing to compare")
        return 0
    bad = find_unbumped(base, _snapshot(REPO / "src"))
    for name in bad:
        print(
            f"prompt {name!r}: text changed since {args.base} but its version is still "
            f"{base[name][0]}. Bump it in the Prompt(...) definition, then update the pin in "
            "tests/test_prompts.py (CONTRIBUTING.md, 'Prompts are versioned').",
            file=sys.stderr,
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
