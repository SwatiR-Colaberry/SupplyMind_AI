"""Google Sheets connector for the data integration layer.

Every attempt is logged with a timestamp and outcome. Only transient API
errors (rate limiting, upstream 5xx) are retried; auth failures and a
missing/inaccessible spreadsheet are not, since retrying cannot fix them.
"""

from __future__ import annotations

import time
from typing import Any

import google.auth.exceptions
import gspread
import gspread.exceptions

from data_integration.config import GoogleSheetsConfig, load_google_sheets_config
from data_integration.logging_setup import get_logger, log_integration_attempt

REQUEST_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

logger = get_logger()


class SheetsIntegrationError(RuntimeError):
    """Raised when Google Sheets data cannot be retrieved after retries."""


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, gspread.exceptions.APIError):
        return False
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in _RETRYABLE_STATUS_CODES


def _authenticate(config: GoogleSheetsConfig):
    client = gspread.service_account(filename=config.service_account_json_path)
    client.http_client.set_timeout(REQUEST_TIMEOUT_SECONDS)
    return client


def fetch_rows(
    spreadsheet_id: str,
    worksheet_name: str,
    config: GoogleSheetsConfig | None = None,
) -> list[dict[str, Any]]:
    """Read all rows of a worksheet and return them as dicts keyed by header row.

    Authentication happens once — it is not retried, since credential
    problems don't resolve themselves. Only the worksheet fetch (which can
    hit transient API errors) is retried, reusing the same client.
    """
    cfg = config or load_google_sheets_config()
    last_error: Exception | None = None

    try:
        client = _authenticate(cfg)
    except (google.auth.exceptions.GoogleAuthError, FileNotFoundError) as exc:
        log_integration_attempt(
            logger,
            source="google_sheets",
            outcome="failure",
            error_class=exc.__class__.__name__,
            context={"retryable": False, "worksheet": worksheet_name, "stage": "authenticate"},
        )
        raise SheetsIntegrationError(
            f"Google Sheets authentication failed: {exc.__class__.__name__}"
        ) from exc

    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.monotonic()
        try:
            spreadsheet = client.open_by_key(spreadsheet_id)
            worksheet = spreadsheet.worksheet(worksheet_name)
            rows = worksheet.get_all_records()
            log_integration_attempt(
                logger,
                source="google_sheets",
                outcome="success",
                duration_ms=(time.monotonic() - start) * 1000,
                context={"row_count": len(rows), "attempt": attempt, "worksheet": worksheet_name},
            )
            return rows
        except (
            gspread.exceptions.SpreadsheetNotFound,
            gspread.exceptions.WorksheetNotFound,
        ) as exc:
            log_integration_attempt(
                logger,
                source="google_sheets",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "retryable": False, "worksheet": worksheet_name},
            )
            raise SheetsIntegrationError(
                f"Google Sheets access failed: {exc.__class__.__name__}"
            ) from exc
        except Exception as exc:
            last_error = exc
            retryable = _is_retryable(exc)
            log_integration_attempt(
                logger,
                source="google_sheets",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "retryable": retryable},
            )
            if not retryable:
                raise SheetsIntegrationError(
                    f"Google Sheets query failed: {exc.__class__.__name__}"
                ) from exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)

    raise SheetsIntegrationError(
        f"Google Sheets unavailable after {MAX_ATTEMPTS} attempts"
    ) from last_error
