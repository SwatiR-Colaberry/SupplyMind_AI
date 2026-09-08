import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from data_console.sheet_fetcher import InvalidSheetUrlError, SheetFetchError, fetch_sheet_csv


def _mock_response(body: bytes, content_type: str = "text/csv"):
    response = MagicMock()
    response.read.return_value = body
    response.headers = {"Content-Type": content_type}
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_fetch_sheet_csv_returns_the_response_body():
    with patch("data_console.sheet_fetcher.urllib.request.urlopen", return_value=_mock_response(b"a,b\n1,2\n")):
        assert fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv") == b"a,b\n1,2\n"


@pytest.mark.parametrize("url", [
    "http://docs.google.com/spreadsheets/d/abc/export?format=csv",  # not https
    "https://evil.example.com/export?format=csv",                   # wrong host
    "https://docs.google.com.evil.com/export?format=csv",           # lookalike host
    "not a url at all",
    "",
])
def test_fetch_sheet_csv_rejects_anything_that_is_not_an_https_docs_google_com_link(url):
    with patch("data_console.sheet_fetcher.urllib.request.urlopen") as mock_urlopen:
        with pytest.raises(InvalidSheetUrlError):
            fetch_sheet_csv(url)
    mock_urlopen.assert_not_called()


def test_fetch_sheet_csv_raises_on_an_http_error():
    with patch(
        "data_console.sheet_fetcher.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError("url", 404, "Not Found", {}, None),
    ):
        with pytest.raises(SheetFetchError):
            fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")


def test_fetch_sheet_csv_raises_when_the_host_is_unreachable():
    with patch(
        "data_console.sheet_fetcher.urllib.request.urlopen",
        side_effect=urllib.error.URLError("network is unreachable"),
    ):
        with pytest.raises(SheetFetchError):
            fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")


def test_fetch_sheet_csv_raises_when_the_response_is_an_html_page_not_csv():
    # The realistic "you pasted a link to a sheet that isn't actually public"
    # case - Google serves an HTML sign-in page with a plain 200 status.
    html = b"<!DOCTYPE html><html><body>Sign in</body></html>"
    with patch("data_console.sheet_fetcher.urllib.request.urlopen", return_value=_mock_response(html, "text/html")):
        with pytest.raises(SheetFetchError):
            fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")


def test_fetch_sheet_csv_raises_when_content_type_is_csv_but_body_is_still_html():
    # Belt and suspenders: sniff the body too, not just the header, in case a
    # misconfigured response claims text/csv but is actually an HTML page.
    html = b"<html><body>Sign in required</body></html>"
    with patch("data_console.sheet_fetcher.urllib.request.urlopen", return_value=_mock_response(html, "text/csv")):
        with pytest.raises(SheetFetchError):
            fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")


def test_fetch_sheet_csv_raises_when_the_response_exceeds_the_size_cap():
    with patch("data_console.sheet_fetcher.MAX_SHEET_BYTES", 5), \
         patch("data_console.sheet_fetcher.urllib.request.urlopen", return_value=_mock_response(b"a,b,c\n1,2,3\n")):
        with pytest.raises(SheetFetchError):
            fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")


def test_fetch_sheet_csv_passes_an_explicit_timeout():
    with patch("data_console.sheet_fetcher.urllib.request.urlopen", return_value=_mock_response(b"a,b\n1,2\n")) as mock_urlopen:
        fetch_sheet_csv("https://docs.google.com/spreadsheets/d/abc/export?format=csv")
    _, kwargs = mock_urlopen.call_args
    assert kwargs["timeout"] > 0
