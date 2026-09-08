"""Durable local storage for CSV files uploaded through "Map Your Data".

Every upload is saved to disk under a generated id rather than held only in
the browser's memory, so a saved mapping survives a page reload without
asking the person to re-upload the same file. Not committed to the repo
(see .gitignore): an uploaded file is real, arbitrary user data, which has
no business riding along in a public portfolio repo.

Headers are read fresh from disk on every probe/validate call rather than
cached anywhere - the same "never trust a schema snapshot the browser last
saw" principle schema_inspector.py and preview_runner.py already follow for
a live Postgres table, applied here to a saved file instead.
"""

from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"

# A CSV mapped into one of this console's 3 known datasets is a handful of
# columns, not a data warehouse export - this cap exists purely so a
# mistaken multi-gigabyte upload can't exhaust this single-threaded dev
# server's memory, the same reasoning MAX_REQUEST_BODY_BYTES already
# applies to every other request body in serve_data_console.py.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class UnknownUploadError(LookupError):
    """Raised when a file_id doesn't correspond to a saved upload."""


def _path_for(file_id: str) -> Path:
    # file_id is always this module's own uuid4().hex output (never
    # user-supplied free text - the HTTP layer only ever passes back an id
    # this module handed out), so there is no path-traversal surface to
    # guard against here the way there would be for a caller-chosen name.
    return UPLOADS_DIR / f"{file_id}.csv"


def save_upload(filename: str, content: bytes) -> str:
    """Saves `content` under a fresh id and returns that id."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    _path_for(file_id).write_bytes(content)
    return file_id


def _read_text(file_id: str) -> str:
    path = _path_for(file_id)
    if not path.exists():
        raise UnknownUploadError(f"no uploaded file with id {file_id!r}")
    # utf-8-sig tolerates (and strips) a leading byte-order-mark, which
    # Excel's own "Save as CSV UTF-8" adds and plain utf-8 decoding would
    # otherwise leave stuck to the first header name.
    return path.read_text(encoding="utf-8-sig")


def read_upload_columns(file_id: str) -> list[str]:
    """Returns just the header row - cheap even for a large file, since csv.reader
    is only asked to yield the first row."""
    reader = csv.reader(io.StringIO(_read_text(file_id)))
    try:
        return next(reader)
    except StopIteration:
        return []


def read_upload_rows(file_id: str) -> list[dict[str, str]]:
    """Returns every data row (header row excluded) as a list of dicts keyed by column name."""
    reader = csv.DictReader(io.StringIO(_read_text(file_id)))
    return list(reader)
