"""Load and validate ``metrics.yaml`` files."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from analyst_agent.semantic.schema import Metric, MetricsFile


class MetricsLoadError(ValueError):
    """A metrics file is missing, unparseable or fails schema validation."""


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise MetricsLoadError(f"{path}: cannot read file: {e}") from e
    except yaml.YAMLError as e:
        raise MetricsLoadError(f"{path}: invalid YAML: {e}") from e


def _is_other_kind(raw: object) -> bool:
    """A YAML file that declares a ``kind`` other than ``metrics`` (e.g. ``retrieval_eval``)."""
    return isinstance(raw, dict) and "kind" in raw and raw["kind"] != "metrics"


def load_metrics_file(path: Path | str) -> list[Metric]:
    """Parse and validate a single metrics YAML file."""
    path = Path(path)
    return _parse(path, _read(path))


def _parse(path: Path, raw: object) -> list[Metric]:
    if not isinstance(raw, dict):
        raise MetricsLoadError(f"{path}: expected a mapping with a 'metrics' list")
    try:
        return list(MetricsFile.model_validate(raw).metrics)
    except ValidationError as e:
        raise MetricsLoadError(f"{path}: {e}") from e


def _expand(paths: Iterable[Path | str]) -> list[tuple[Path, bool]]:
    """``(file, from_directory)`` pairs."""
    files: list[tuple[Path, bool]] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += [(f, True) for f in sorted([*p.glob("*.yaml"), *p.glob("*.yml")])]
        elif p.exists():
            files.append((p, False))
        else:
            raise MetricsLoadError(f"{p}: no such file or directory")
    return files


def load_metrics(paths: Path | str | Iterable[Path | str]) -> list[Metric]:
    """Load metrics from files and/or directories (``*.yaml`` / ``*.yml``).

    Metric names must be unique across everything loaded. When scanning a directory, YAML files
    that declare a different top-level ``kind`` (such as ``retrieval_eval.yaml``) are skipped;
    passing such a file explicitly is an error.
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    metrics: list[Metric] = []
    origin: dict[str, Path] = {}
    for f, from_dir in _expand(paths):
        raw = _read(f)
        if from_dir and _is_other_kind(raw):
            continue
        for m in _parse(f, raw):
            if m.name in origin:
                raise MetricsLoadError(
                    f"{f}: duplicate metric name {m.name!r} (also defined in {origin[m.name]})"
                )
            origin[m.name] = f
            metrics.append(m)
    return metrics
