"""Fetches a public Google Sheets CSV export over plain HTTPS.

Covers the "publish to web as CSV" flow (File > Share > Publish to web,
pick one specific tab, choose CSV) and the equivalent "Anyone with the link
can view" + export URL - both are an unauthenticated GET once the sheet's
owner has made that one tab publicly readable. No OAuth, no credentials,
no new dependency: Python's stdlib urllib does the whole job.

Restricted to https://docs.google.com URLs only - this server fetches
whatever URL it is handed, so accepting arbitrary hosts here would turn a
local mapping tool into an open URL-fetch proxy (a real SSRF-adjacent risk
even for a single-operator local tool, since nothing else in this codebase
makes outbound calls to caller-supplied URLs at all). Every request carries
an explicit timeout and a capped read size, per this repo's own rule that
every outbound call must bound both. A single attempt, not retried: the two
realistic failure causes - a mistyped or unpublished link, and content that
turns out not to be real CSV at all - are both input problems a retry
cannot fix, unlike a transient database connection blip.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlparse

FETCH_TIMEOUT_SECONDS = 10

# Same cap file_store.MAX_UPLOAD_BYTES applies to an uploaded file - a
# dataset mapped into one of this console's 3 known slots is a handful of
# columns, not a data warehouse export.
MAX_SHEET_BYTES = 10 * 1024 * 1024

_ALLOWED_HOST = "docs.google.com"


class InvalidSheetUrlError(ValueError):
    """Raised when a URL isn't an https://docs.google.com link at all."""


class SheetFetchError(RuntimeError):
    """Raised when a docs.google.com URL can't be fetched, exceeds MAX_SHEET_BYTES,
    or doesn't actually return CSV - most commonly because the sheet (or that
    specific tab) isn't really shared publicly, so Google serves an HTML sign-in
    page back with a plain 200 status instead of an error status."""


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise InvalidSheetUrlError("a Google Sheets link is required")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != _ALLOWED_HOST:
        raise InvalidSheetUrlError(
            f"only an https://{_ALLOWED_HOST} link is accepted - "
            "paste the CSV export/publish link for one specific tab"
        )


def _looks_like_html(content: bytes, content_type: str) -> bool:
    if "text/html" in content_type.lower():
        return True
    return content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))


def fetch_sheet_csv(url: str) -> bytes:
    """Returns the raw CSV bytes at `url`.

    Raises InvalidSheetUrlError if `url` isn't an https://docs.google.com
    link (checked before any network call is made), or SheetFetchError if
    the request fails, the response exceeds MAX_SHEET_BYTES, or the
    response isn't real CSV.
    """
    _validate_url(url)

    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content = response.read(MAX_SHEET_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise SheetFetchError(f"Google returned an error ({exc.code}) for that link") from exc
    except urllib.error.URLError as exc:
        raise SheetFetchError(f"could not reach that link: {exc.reason}") from exc

    if len(content) > MAX_SHEET_BYTES:
        raise SheetFetchError(f"that sheet is larger than the {MAX_SHEET_BYTES}-byte limit")

    if _looks_like_html(content, content_type):
        raise SheetFetchError(
            "that link did not return CSV data - make sure the sheet (or that specific tab) is "
            'shared as "Anyone with the link can view", or published to the web as CSV'
        )

    return content
