"""HTTP serving layer (FastAPI) for the data analyst agent."""

from .app import create_app

__all__ = ["create_app"]
