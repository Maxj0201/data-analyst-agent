"""The committed benchmark must be exactly what the seeded generator produces."""

from __future__ import annotations

import filecmp
import importlib.util
import sys
from pathlib import Path

from analyst_agent.eval.schema import DEFAULT_BENCHMARK_ROOT


def _load_generator():
    path = DEFAULT_BENCHMARK_ROOT / "data" / "generate.py"
    spec = importlib.util.spec_from_file_location("bench_generate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


GENERATED_DIRS = ("data", "tasks")  # metrics/ is hand-written, not generated


def _files(root: Path) -> set[Path]:
    return {
        p.relative_to(root)
        for d in GENERATED_DIRS
        for p in (root / d).rglob("*")
        if p.is_file() and p.suffix in {".csv", ".yaml"}
    }


def test_generator_is_deterministic_and_in_sync(tmp_path: Path) -> None:
    _load_generator().generate(tmp_path)
    committed = _files(DEFAULT_BENCHMARK_ROOT)
    assert _files(tmp_path) == committed
    mismatched = [
        str(p)
        for p in committed
        if not filecmp.cmp(tmp_path / p, DEFAULT_BENCHMARK_ROOT / p, shallow=False)
    ]
    assert not mismatched, f"regenerate with benchmarks/data/generate.py: {mismatched}"


def test_support_tickets_contain_injection_payloads() -> None:
    text = (DEFAULT_BENCHMARK_ROOT / "data/support/support_tickets.csv").read_text()
    for needle in [
        "ignore previous instructions",
        "drop table",
        "print the system prompt",
        "include every customer email",
        "999 hours",
        "evil.example.com",
    ]:
        assert needle in text.lower(), needle
