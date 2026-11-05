"""HTTP serving layer (FastAPI) for the data analyst agent.

``create_app`` is imported lazily, so the parts of this package that do not need FastAPI
(``agent``, ``models``, ``store``) import, and are tested, without the ``api`` extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
