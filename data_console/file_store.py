"""Durable local storage for CSV files uploaded through "Map Your Data",
including CSVs extracted from an uploaded zip bundle.

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
import zipfile
from pathlib import Path

UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"

# A real-world CSV export (e.g. a full orders/inventory dump) can run into
# the tens of megabytes even though it only has a handful of columns this
# console actually needs - a plain row-count problem, not a sign of a
# mistaken upload. This cap exists purely so a truly absurd multi-gigabyte
# upload can't exhaust this single-threaded dev server's memory, the same
# reasoning MAX_REQUEST_BODY_BYTES already applies to every other request
# body in serve_data_console.py - it isn't meant to reject an ordinary large
# dataset. Also used as the per-member decompressed-size guard for a zip
# upload (see extract_csvs_from_zip) - a "zip bomb" is a tiny compressed
# file that expands to something huge, so the raw upload's own size cap
# alone wouldn't catch it.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# This console only ever needs to fill 3 dataset slots - a zip with more
# members than this is almost certainly not "one CSV per dataset" and is
# rejected outright rather than silently truncated.
MAX_ZIP_MEMBERS = 10


class UnknownUploadError(LookupError):
    """Raised when a file_id doesn't correspond to a saved upload."""


class UnsafeArchiveError(ValueError):
    """Raised when a zip upload fails a safety check: not a valid zip, too many
    members, a member too large once decompressed, or no .csv members at all."""


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


def parse_csv_columns(text: str) -> list[str]:
    """Pure - just the header row of raw CSV text, without reading/fetching anything.
    Cheap even for a large text, since csv.reader is only asked to yield the first row.
    Shared with sheet_mapping_service.py, whose CSV text comes from a network fetch
    rather than a saved file - the parsing itself doesn't care where the text came from."""
    reader = csv.reader(io.StringIO(text))
    try:
        return next(reader)
    except StopIteration:
        return []


def parse_csv_rows(text: str) -> list[dict[str, str]]:
    """Pure - every data row of raw CSV text (header row excluded) as dicts keyed by column name."""
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def read_upload_columns(file_id: str) -> list[str]:
    """Returns just the header row of a saved upload."""
    return parse_csv_columns(_read_text(file_id))


def read_upload_rows(file_id: str) -> list[dict[str, str]]:
    """Returns every data row (header row excluded) of a saved upload, as dicts keyed by column name."""
    return parse_csv_rows(_read_text(file_id))


def extract_csvs_from_zip(content: bytes) -> list[tuple[str, str]]:
    """Extracts every .csv member from a zip archive, saving each one individually
    via save_upload() exactly as if it had been uploaded on its own.

    Returns a list of (file_id, member_filename) pairs, one per .csv member
    found - lets a single zip fill more than one of this console's 3 dataset
    slots without uploading three separate files.

    Raises UnsafeArchiveError if: the archive isn't a valid zip; it has more
    than MAX_ZIP_MEMBERS entries; any .csv member exceeds MAX_UPLOAD_BYTES
    once decompressed; or it contains no .csv members at all.

    A member's own path inside the archive (e.g. "data/orders.csv") is used
    only as display text via its basename - it is never treated as a
    filesystem path this module writes to, so there is no zip-slip /
    path-traversal surface here regardless of what a member's name claims to
    be (every file this function writes still gets a fresh save_upload() id).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, EOFError) as exc:
        raise UnsafeArchiveError("not a valid zip file") from exc

    members = [info for info in archive.infolist() if not info.is_dir()]
    if len(members) > MAX_ZIP_MEMBERS:
        raise UnsafeArchiveError(f"zip contains more than {MAX_ZIP_MEMBERS} files")

    csv_members = [info for info in members if info.filename.lower().endswith(".csv")]
    if not csv_members:
        raise UnsafeArchiveError("zip contains no .csv files")

    oversized = [info.filename for info in csv_members if info.file_size > MAX_UPLOAD_BYTES]
    if oversized:
        raise UnsafeArchiveError(
            f"file(s) exceed the {MAX_UPLOAD_BYTES}-byte limit once extracted: {', '.join(oversized)}"
        )

    results: list[tuple[str, str]] = []
    for info in csv_members:
        display_name = Path(info.filename).name
        file_id = save_upload(display_name, archive.read(info))
        results.append((file_id, display_name))
    return results
