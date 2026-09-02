"""Live BTS Border Crossing Entry Data connector (Socrata SODA API, no auth required).

Queries data.bts.gov's public "Border Crossing Entry Data" dataset
(resource id keg4-3bc2 - U.S. Customs and Border Protection counts of
trucks/trains/containers/passengers/pedestrians entering the U.S. at
every port of entry, updated monthly) at request time - genuinely live,
not a static snapshot. Truck-crossing volume is a well-established
real-world proxy for freight/supply-chain shipment volume, used here as
a live demand-history source for root cause analysis (STORY-007) instead
of only synthetic or locally-seeded data.

Every attempt is logged with a timestamp and outcome, same shape as
postgres_connector.py/sheets_connector.py. Only transient errors (rate
limiting, upstream 5xx, connection/timeout) are retried; a malformed
query or an unparseable response is not, since retrying cannot fix them.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

import requests

from data_integration.logging_setup import get_logger, log_integration_attempt

BTS_BORDER_CROSSING_ENDPOINT = "https://data.bts.gov/resource/keg4-3bc2.json"
REQUEST_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

DEFAULT_MEASURE = "Trucks"
DEFAULT_LOOKBACK_MONTHS = 24

logger = get_logger()


class BtsBorderCrossingIntegrationError(RuntimeError):
    """Raised when the live BTS Border Crossing dataset can't be retrieved after retries."""


def _lookback_cutoff(lookback_months: int, as_of: date) -> date:
    # Calendar-month arithmetic done by walking back whole months from the
    # 1st, not `timedelta(days=30*n)` - the latter drifts against real
    # month boundaries over a 24-month lookback, which would silently
    # shift how much history a caller actually gets.
    year, month = as_of.year, as_of.month
    for _ in range(lookback_months):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return date(year, month, 1)


def _fetch_raw_rows(measure: str, lookback_months: int, as_of: date) -> list[dict[str, Any]]:
    cutoff = _lookback_cutoff(lookback_months, as_of)
    params = {
        "measure": measure,
        "$where": f"date >= '{cutoff.isoformat()}T00:00:00.000'",
        "$select": "date,value",
        "$limit": "50000",
    }
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.monotonic()
        try:
            response = requests.get(BTS_BORDER_CROSSING_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            rows = response.json()
            log_integration_attempt(
                logger,
                source="bts_border_crossing",
                outcome="success",
                duration_ms=(time.monotonic() - start) * 1000,
                context={"row_count": len(rows), "attempt": attempt, "measure": measure},
            )
            return rows
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            retryable = status_code in _RETRYABLE_STATUS_CODES
            last_error = exc
            log_integration_attempt(
                logger,
                source="bts_border_crossing",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "retryable": retryable, "status_code": status_code},
            )
            if not retryable:
                raise BtsBorderCrossingIntegrationError(
                    f"BTS border crossing query failed: HTTP {status_code}"
                ) from exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            log_integration_attempt(
                logger,
                source="bts_border_crossing",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "retryable": True},
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        except ValueError as exc:  # response.json() malformed
            log_integration_attempt(
                logger,
                source="bts_border_crossing",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "retryable": False},
            )
            raise BtsBorderCrossingIntegrationError(f"BTS border crossing response was not valid JSON: {exc}") from exc

    raise BtsBorderCrossingIntegrationError(
        f"BTS border crossing API unavailable after {MAX_ATTEMPTS} attempts"
    ) from last_error


def fetch_monthly_truck_crossing_history(
    measure: str = DEFAULT_MEASURE,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Live national monthly truck-crossing totals, shaped as demand_history rows.

    This dataset already reports one row per port per month, so this sums
    every port's count into one national total per month, then returns
    rows in the exact {"order_date", "quantity"} shape
    forecasting/aggregation.py's aggregate_monthly_demand() expects - the
    same input contract RiskDetectionAgent's/RootCauseAnalysisAgent's
    "demand_history" context key already uses, so this plugs in downstream
    with zero changes.

    Handles: a row missing date/value, or a value that isn't numeric
    (skipped, not fatal - mirrors aggregate_monthly_demand()'s own
    "skip, don't crash on one bad row" behavior for the exact same
    reason). Raises BtsBorderCrossingIntegrationError (propagated from
    _fetch_raw_rows) when the live API itself is unreachable or rejects
    the query - a data-source availability problem, not a data-shape one.
    """
    raw_rows = _fetch_raw_rows(measure, lookback_months, as_of or datetime.now(timezone.utc).date())

    totals: dict[str, float] = defaultdict(float)
    for row in raw_rows:
        raw_date = row.get("date")
        raw_value = row.get("value")
        if raw_date is None or raw_value is None:
            continue
        try:
            quantity = float(raw_value)
        except (TypeError, ValueError):
            continue
        period = raw_date[:7]  # "YYYY-MM-DDT00:00:00.000" -> "YYYY-MM"
        totals[period] += quantity

    return [{"order_date": f"{period}-01", "quantity": total} for period, total in sorted(totals.items())]
