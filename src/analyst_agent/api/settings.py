"""Configuration for the serving layer.

All settings are read from ``AGENT_*`` environment variables (or a ``.env`` file), the same
prefix the agent core uses, so a single secret/env bundle configures the whole service.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    """Root for service state. Uploads live in ``<data_dir>/uploads``."""

    artifacts_dir: Path = Path("artifacts")
    """Directory the agent writes charts to; served read-only under ``/artifacts``."""

    job_db_path: Path | None = Path("data/jobs.sqlite3")
    """SQLite fallback for the job store. Set to an empty string to run memory-only."""

    job_memory_limit: int = Field(default=500, ge=1)
    """Max jobs kept in memory; older finished jobs are evicted and served from SQLite."""

    max_concurrent_jobs: int = Field(default=4, ge=1)
    max_upload_mb: int = Field(default=50, ge=1)

    langfuse_enabled: bool = False
    """Send traces to Langfuse (keys come from the standard ``LANGFUSE_*`` env vars)."""

    cors_origins: list[str] = Field(default_factory=list)

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"
