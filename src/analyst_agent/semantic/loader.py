"""Load and validate ``metrics.yaml`` files."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from analyst_agent.semantic.schema import Metric, MetricsFile


class MetricsLoadError(ValueError):
    """A metrics file is missing, unparseable or fails schema validation."""


def load_metrics_file(path: Path | str) -> list[Metric]:
    """Parse and validate a single metrics YAML file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise MetricsLoadError(f"{path}: cannot read file: {e}") from e
    except yaml.YAMLError as e:
        raise MetricsLoadError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise MetricsLoadError(f"{path}: expected a mapping with a 'metrics' list")
    try:
        return list(MetricsFile.model_validate(raw).metrics)
    except ValidationError as e:
        raise MetricsLoadError(f"{path}: {e}") from e


def _expand(paths: Iterable[Path | str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted([*p.glob("*.yaml"), *p.glob("*.yml")])
        elif p.exists():
            files.append(p)
        else:
            raise MetricsLoadError(f"{p}: no such file or directory")
    return files


def load_metrics(paths: Path | str | Iterable[Path | str]) -> list[Metric]:
    """Load metrics from files and/or directories (``*.yaml`` / ``*.yml``).

    Metric names must be unique across everything loaded.
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    metrics: list[Metric] = []
    origin: dict[str, Path] = {}
    for f in _expand(paths):
        for m in load_metrics_file(f):
            if m.name in origin:
                raise MetricsLoadError(
                    f"{f}: duplicate metric name {m.name!r} (also defined in {origin[m.name]})"
                )
            origin[m.name] = f
            metrics.append(m)
    return metrics
