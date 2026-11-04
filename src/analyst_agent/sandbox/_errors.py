"""Stable error kinds. Errors are reported as ``"<Kind>: <message>"`` strings in
``SqlResult.error`` / ``PyResult.error``; for Python runtime errors raised by user code the
kind is the exception class name (e.g. ``"ZeroDivisionError: division by zero"``)."""

STATEMENT_NOT_ALLOWED = "StatementNotAllowed"
TIMEOUT = "Timeout"
MEMORY_LIMIT = "MemoryLimitExceeded"
PERMISSION_DENIED = "PermissionDenied"
QUERY_ERROR = "QueryError"
NO_DATA = "NoData"
DATA_LOAD_ERROR = "DataLoadError"
PROCESS_CRASHED = "ProcessCrashed"


def fmt(kind: str, message: str) -> str:
    return f"{kind}: {message}"


def kind_of(error: str | None) -> str | None:
    """Extract the kind prefix from an error string (``None`` if there is no error)."""
    if not error:
        return None
    return error.split(":", 1)[0].strip()
