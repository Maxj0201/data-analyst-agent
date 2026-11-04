"""CSV dataset uploads, stored as ``<uploads_dir>/<dataset_id>.csv`` plus a JSON sidecar."""

from __future__ import annotations

import csv
import io
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .models import DatasetInfo

_DATASET_ID_RE = re.compile(r"^ds_[0-9a-f]{16}$")
_CHUNK = 1024 * 1024


class DatasetStore:
    def __init__(self, uploads_dir: Path, max_bytes: int) -> None:
        self.dir = uploads_dir
        self.max_bytes = max_bytes
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, dataset_id: str) -> Path | None:
        if not _DATASET_ID_RE.match(dataset_id):
            return None
        path = self.dir / f"{dataset_id}.csv"
        return path if path.is_file() else None

    def info(self, dataset_id: str) -> DatasetInfo | None:
        if self.path_for(dataset_id) is None:
            return None
        return DatasetInfo.model_validate_json((self.dir / f"{dataset_id}.json").read_text())

    async def save(self, upload: UploadFile) -> DatasetInfo:
        filename = Path(upload.filename or "upload.csv").name
        if not filename.lower().endswith(".csv"):
            raise HTTPException(status_code=415, detail="Only .csv files are accepted.")

        dataset_id = "ds_" + uuid.uuid4().hex[:16]
        dest = self.dir / f"{dataset_id}.csv"
        tmp = dest.with_suffix(".part")
        size = 0
        try:
            with tmp.open("wb") as fh:
                while chunk := await upload.read(_CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit.",
                        )
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty.")
            columns = _validate_csv(tmp)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

        info = DatasetInfo(
            dataset_id=dataset_id,
            filename=filename,
            size_bytes=size,
            columns=columns,
            created_at=time.time(),
        )
        (self.dir / f"{dataset_id}.json").write_text(info.model_dump_json())
        return info


def _validate_csv(path: Path) -> list[str]:
    """Cheap structural check: UTF-8 text with a header row and at least one column."""
    with path.open("rb") as fh:
        head = fh.read(64 * 1024)
    if b"\x00" in head:
        raise HTTPException(status_code=400, detail="File looks binary, not CSV.")
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # A multi-byte char may be split at the 64 KiB boundary; only fail on real errors.
        if exc.start < len(head) - 4:
            raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc
        text = head[: exc.start].decode("utf-8-sig")
    try:
        header = next(csv.reader(io.StringIO(text)))
    except (StopIteration, csv.Error) as exc:
        raise HTTPException(status_code=400, detail="Could not parse CSV header.") from exc
    columns = [c.strip() for c in header]
    if not any(columns):
        raise HTTPException(status_code=400, detail="CSV has no header row.")
    return columns
