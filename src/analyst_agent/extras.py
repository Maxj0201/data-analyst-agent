"""Optional extras: one error type and one message format for "this needs extra X".

Every entry point that can hit a missing extra catches :class:`MissingExtraError` and prints
its message as a single line instead of a traceback (fresh-clone audit,
docs/fresh-clone-audit.md). Library callers can still catch it as an ``ImportError``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


class MissingExtraError(ImportError):
    """``what`` needs the optional ``extra``; the message says how to install it."""

    def __init__(self, extra: str, what: str) -> None:
        self.extra = extra
        super().__init__(
            f"{what} needs the optional '{extra}' extra: uv sync --extra {extra} "
            f"(or pip install 'analyst-agent[{extra}]')"
        )


def mcp_main(argv: Sequence[str] | None = None) -> None:
    """``analyst-agent-mcp`` console script: the MCP server, or a one-line hint without ``mcp``."""
    try:
        from analyst_agent.mcp_server import main
    except MissingExtraError as exc:
        print(f"analyst-agent-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    main(argv)
