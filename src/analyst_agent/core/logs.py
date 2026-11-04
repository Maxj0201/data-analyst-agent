"""Structured logging with run/job correlation (stdlib ``logging`` only).

Every log record gets the fields bound with :func:`log_context` attached, so a line written by
the sandbox while a job is running carries the same ``job_id`` and ``run_id`` as the API's and
the loop's lines for that job:

* the API binds ``job_id`` around each job (``api/jobs.py``),
* ``AgentLoop.run`` binds ``run_id`` around the run,
* the eval runner binds ``task_id``.

Context lives in a :class:`contextvars.ContextVar`, so it is per thread / per asyncio task and
concurrent jobs never see each other's ids. Code that hands work to another thread must copy the
context itself (``contextvars.copy_context().run``); the agent loop and the sandbox run tools in
the caller's thread, so nothing extra is needed there.

:func:`configure_logging` installs one stderr handler on the root logger: JSON lines when
``AGENT_LOG_JSON_ENABLED=true`` (for log shippers), human-readable text otherwise. In both modes
the context fields are printed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import IO, Any

_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "analyst_agent_log_context", default=MappingProxyType({})
)

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)
_HANDLER_MARK = "_analyst_agent_handler"


def current_context() -> dict[str, Any]:
    """The correlation fields bound in this thread / task."""
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Bind ``fields`` (e.g. ``run_id=...``) to every log record emitted inside the block.

    Nested blocks add to (and may override) the outer fields; ``None`` values are ignored.
    """
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    """Copies the bound context onto each record (``record.run_id``, ``record.job_id``, …)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CONTEXT.get()
        for key, value in ctx.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        record.log_context = dict(ctx)
        return True


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _STANDARD_ATTRS and k != "log_context" and not k.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``msg``, context and extras."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "log_context", None) or {})
        out.update(_extra_fields(record))
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    """``time level logger: message [run_id=… job_id=…]``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = {**(getattr(record, "log_context", None) or {}), **_extra_fields(record)}
        if fields:
            line += " [" + " ".join(f"{k}={v}" for k, v in fields.items()) + "]"
        return line


def configure_logging(
    *, json_logs: bool | None = None, level: str | None = None, stream: IO[str] | None = None
) -> logging.Handler:
    """Install (or replace) the package's handler on the root logger. Idempotent.

    ``json_logs`` / ``level`` default to ``AGENT_LOG_JSON_ENABLED`` / ``AGENT_LOG_LEVEL``.
    """
    if json_logs is None or level is None:
        from analyst_agent.core.config import Settings

        settings = Settings()
        json_logs = settings.log_json_enabled if json_logs is None else json_logs
        level = settings.log_level if level is None else level
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARK, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    handler.addFilter(ContextFilter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler
