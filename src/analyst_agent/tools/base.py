"""Shared tool types."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from analyst_agent.sandbox import PyResult, SqlResult


class SandboxLike(Protocol):
    def run_sql(self, query: str) -> SqlResult: ...
    def run_python(self, code: str) -> PyResult: ...


@dataclass
class ToolContext:
    """Per-run tool state.

    Data access goes through ``sandbox`` when set (a per-run object such as
    ``analyst_agent.sandbox.create_sandbox(...)``, needed when several runs share a process, e.g.
    the API's job pool or the eval runner); otherwise through the module-level
    ``analyst_agent.sandbox.run_sql`` / ``run_python`` (see CLAUDE.md), which is what the CLI uses.
    """

    tables: list[str]
    artifacts_dir: Path = Path("artifacts")
    sandbox: SandboxLike | None = None

    def run_sql(self, query: str) -> SqlResult:
        if self.sandbox is not None:
            return self.sandbox.run_sql(query)
        from analyst_agent import sandbox

        return sandbox.run_sql(query)

    def run_python(self, code: str) -> PyResult:
        if self.sandbox is not None:
            return self.sandbox.run_python(code)
        from analyst_agent import sandbox

        return sandbox.run_python(code)


def table_name_for(path: Path) -> str:
    """File name -> SQL-friendly table name (``Sales 2024.csv`` -> ``sales_2024``)."""
    name = re.sub(r"\W+", "_", path.stem).strip("_").lower() or "data"
    return f"t_{name}" if name[0].isdigit() else name


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    terminate: bool = False


Handler = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
