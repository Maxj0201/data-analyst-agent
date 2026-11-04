"""CSV dataset uploads.

Layout: ``<uploads_dir>/<dataset_id>/<safe original name>.csv`` plus ``meta.json``. Keeping the
original file stem matters: the agent core derives the SQL table name from it, so the model
sees ``sales`` rather than an opaque id.
"""

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
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_filename(raw: str | None) -> str:
    name = Path(raw or "").name
    stem = _UNSAFE_CHARS.sub("_", Path(name).stem).strip("._")[:80] or "data"
    return f"{stem}.csv"


class DatasetStore:
    def __init__(self, uploads_dir: Path, max_bytes: int) -> None:
        self.dir = uploads_dir
        self.max_bytes = max_bytes
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, dataset_id: str) -> Path | None:
        if not _DATASET_ID_RE.match(dataset_id):
            return None
        info = self.info(dataset_id)
        if info is None:
            return None
        path = self.dir / dataset_id / info.stored_name
        return path if path.is_file() else None

    def info(self, dataset_id: str) -> DatasetInfo | None:
        if not _DATASET_ID_RE.match(dataset_id):
            return None
        meta = self.dir / dataset_id / "meta.json"
        if not meta.is_file():
            return None
        return DatasetInfo.model_validate_json(meta.read_text())

    async def save(self, upload: UploadFile) -> DatasetInfo:
        filename = Path(upload.filename or "upload.csv").name
        if not filename.lower().endswith(".csv"):
            raise HTTPException(status_code=415, detail="Only .csv files are accepted.")

        dataset_id = "ds_" + uuid.uuid4().hex[:16]
        stored_name = _safe_filename(filename)
        dest_dir = self.dir / dataset_id
        tmp = self.dir / f"{dataset_id}.part"
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
            dest_dir.mkdir()
            tmp.replace(dest_dir / stored_name)
        finally:
            tmp.unlink(missing_ok=True)

        info = DatasetInfo(
            dataset_id=dataset_id,
            filename=filename,
            stored_name=stored_name,
            size_bytes=size,
            columns=columns,
            created_at=time.time(),
        )
        # meta.json is written last: a dataset only "exists" once it is complete.
        (dest_dir / "meta.json").write_text(info.model_dump_json())
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
