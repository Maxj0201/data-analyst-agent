"""Curated analyst vocabulary: query expansion for the sparse (BM25) backend.

Analysts say "staff", "people still employed" or "GMV" where the governed metric says
"headcount", "attrition" or "net revenue". BM25 only matches shared tokens, so such questions can
rank a metric that merely shares incidental words ("current employees" in a tenure definition)
above the one they are about. A vocabulary file maps those everyday phrasings onto the terms the
metric definitions use::

    kind: vocabulary
    version: 1
    concepts:
      - name: headcount
        expands_to: [headcount, staff, workforce]
        phrases: [current employees, how many employees, number of employees, personnel]

When a question contains any of a concept's ``phrases`` (or one of its ``expands_to`` terms), the
``expands_to`` tokens the question does not already contain are scored as extra query terms at a
reduced weight (:class:`~analyst_agent.semantic.retriever.BM25Backend`'s ``expansion_weight``).
Matching uses :func:`~analyst_agent.semantic.retriever.tokenize`, so plurals and stop words are
handled the same way as in the index.

Vocabulary files live next to the metric definitions (``kind: vocabulary`` makes
:func:`~analyst_agent.semantic.loader.load_metrics` skip them) and are found by
:func:`load_vocabulary` with the same file/directory rules. The table is generic analyst
vocabulary; it must never name a specific benchmark question.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from analyst_agent.semantic.loader import MetricsLoadError
from analyst_agent.semantic.retriever import tokenize

KIND = "vocabulary"


class ConceptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    expands_to: list[str] = Field(min_length=1)
    phrases: list[str] = Field(default_factory=list)

    @field_validator("expands_to", "phrases")
    @classmethod
    def _tokenizable(cls, v: list[str]) -> list[str]:
        for p in v:
            if not tokenize(p):
                raise ValueError(f"phrase {p!r} has no searchable tokens (only stop words?)")
        return v


class VocabularyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    version: int = 1
    concepts: list[ConceptSpec] = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v != KIND:
            raise ValueError(f"expected kind: {KIND}, got {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported vocabulary file version {v} (expected 1)")
        return v


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


@dataclass(frozen=True)
class Concept:
    name: str
    triggers: tuple[tuple[str, ...], ...]  # token phrases that activate the concept
    expansion: tuple[str, ...]  # tokens added to the query, in order, without duplicates


class Vocabulary:
    """A set of concepts; :meth:`expand` returns the extra query tokens for a tokenized query."""

    def __init__(self, concepts: Iterable[Concept] = ()) -> None:
        self.concepts: list[Concept] = list(concepts)

    @classmethod
    def from_specs(cls, specs: Iterable[ConceptSpec]) -> Vocabulary:
        concepts = []
        for s in specs:
            expansion = list(dict.fromkeys(t for p in s.expands_to for t in tokenize(p)))
            triggers = dict.fromkeys(tuple(tokenize(p)) for p in [*s.phrases, *s.expands_to])
            concepts.append(Concept(s.name, tuple(triggers), tuple(expansion)))
        return cls(concepts)

    def __len__(self) -> int:
        return len(self.concepts)

    def __bool__(self) -> bool:
        return bool(self.concepts)

    def matched(self, tokens: Sequence[str]) -> list[str]:
        """Names of the concepts a tokenized query activates."""
        return [c.name for c in self.concepts if any(_contains(tokens, t) for t in c.triggers)]

    def expand(self, tokens: Sequence[str]) -> list[str]:
        """Expansion tokens for ``tokens`` (already :func:`tokenize`-d), excluding ones present."""
        present = set(tokens)
        out: dict[str, None] = {}
        for c in self.concepts:
            if any(_contains(tokens, t) for t in c.triggers):
                out.update((t, None) for t in c.expansion if t not in present)
        return list(out)


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise MetricsLoadError(f"{path}: cannot read file: {e}") from e
    except yaml.YAMLError as e:
        raise MetricsLoadError(f"{path}: invalid YAML: {e}") from e


def _is_vocabulary(raw: object) -> bool:
    return isinstance(raw, dict) and raw.get("kind") == KIND


def load_vocabulary_file(path: Path | str) -> list[ConceptSpec]:
    path = Path(path)
    try:
        return list(VocabularyFile.model_validate(_read(path)).concepts)
    except ValidationError as e:
        raise MetricsLoadError(f"{path}: {e}") from e


def load_vocabulary(paths: Path | str | Iterable[Path | str]) -> Vocabulary:
    """Concepts from every ``kind: vocabulary`` file at ``paths`` (files and/or directories).

    Directories are scanned for ``*.yaml`` / ``*.yml``; files of another kind (metric
    definitions, the retrieval eval) are ignored. An explicitly passed file is read only if it
    is a vocabulary file, so the metrics path can be passed as is. Concept names must be unique.
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    specs: list[ConceptSpec] = []
    origin: dict[str, Path] = {}
    for p in map(Path, paths):
        if p.is_dir():
            files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")])
        elif p.exists():
            files = [p]
        else:
            raise MetricsLoadError(f"{p}: no such file or directory")
        for f in files:
            if not _is_vocabulary(_read(f)):
                continue
            for s in load_vocabulary_file(f):
                if s.name in origin:
                    raise MetricsLoadError(
                        f"{f}: duplicate vocabulary concept {s.name!r} (also in {origin[s.name]})"
                    )
                origin[s.name] = f
                specs.append(s)
    return Vocabulary.from_specs(specs)
