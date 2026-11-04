"""Shared tool types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analyst_agent.tools.sandbox_iface import Sandbox


@dataclass
class ToolContext:
    sandbox: Sandbox
    tables: list[str]
    artifacts_dir: Path = Path("artifacts")


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
