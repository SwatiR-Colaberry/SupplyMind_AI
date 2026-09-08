"""Renders a DashboardSnapshot to a self-contained HTML page (STORY-009 / REQ-015).

Every agent-derived string (a tile's headline, the notification banner
text, an error message) is HTML-escaped before being interpolated - that
text ultimately comes from data this repo ingests (SKU/supplier names,
raw exception messages), not from a source this module controls, so it
is untrusted for HTML-injection purposes even though it is not an
external HTTP boundary in the Contract Enforcement Layer's usual sense.

Rendering is defensive at two levels, matching the "Dashboard rendering
failure" / "Data visualization errors" / "User interface display issues"
failure paths this story names explicitly: a single tile that fails to
render is replaced with a fallback error card and logged, rather than
taking down the rest of the page; if page assembly itself fails
unexpectedly, the whole function still returns a minimal, valid fallback
error page rather than raising - a caller building a dashboard must
never crash because rendering broke.
"""

from __future__ import annotations

import html as html_lib

from dashboard.logging_setup import get_logger
from dashboard.metrics import DashboardMetric, DashboardSnapshot

logger = get_logger()

_STATUS_COLORS = {"ok": "#2e7d32", "error": "#c62828"}
_SEVERITY_COLORS = {"critical": "#b71c1c", "high": "#e65100", "medium": "#f9a825", "low": "#757575"}
_DEFAULT_COLOR = "#757575"

# Every agent's `headline` is written for someone who already knows this
# codebase (z-scores, confidence floats, agent jargon) - see metrics.py's
# own docstring on why that text is carried through unchanged rather than
# reworded here. This map is the one plain-English sentence a first-time
# reader needs before deciding whether to open a card's technical detail
# at all - keyed on the same (status, severity) pair render already has
# on hand, so it needs no new data from metrics.py.
_PLAIN_TAKEAWAY = {
    "critical": "Needs immediate attention.",
    "high": "Needs attention soon.",
    "medium": "Worth a look.",
    "low": "Low risk - no action needed.",
}
_PLAIN_TAKEAWAY_OK_DEFAULT = "No risk flagged."
_PLAIN_TAKEAWAY_ERROR = "This couldn't be checked - see details below."


def _esc(value: object) -> str:
    return html_lib.escape(str(value)) if value else ""


def _status_color(is_ok: bool) -> str:
    return _STATUS_COLORS.get("ok" if is_ok else "error", _DEFAULT_COLOR)


def _plain_takeaway(metric: DashboardMetric) -> str:
    """One plain-English sentence a non-technical reader sees before anything else."""
    if metric.status == "error":
        return _PLAIN_TAKEAWAY_ERROR
    if metric.severity is not None:
        return _PLAIN_TAKEAWAY.get(metric.severity, _PLAIN_TAKEAWAY_OK_DEFAULT)
    return _PLAIN_TAKEAWAY_OK_DEFAULT


def _log_render_failure(event: str, exc: Exception, context: dict, correlation_id: str | None = None) -> None:
    extra = {"event": event, "outcome": "failure", "error_class": exc.__class__.__name__, "context": context}
    if correlation_id is not None:
        extra["correlation_id"] = correlation_id
    logger.error(event, extra=extra)


def _render_tile(metric: DashboardMetric) -> str:
    status_color = _STATUS_COLORS.get(metric.status, _DEFAULT_COLOR)

    severity_html = ""
    if metric.severity is not None:
        severity_color = _SEVERITY_COLORS.get(metric.severity, _DEFAULT_COLOR)
        severity_html = f'<span class="badge" style="background:{severity_color}">{_esc(metric.severity)}</span>'

    confidence_html = ""
    if metric.confidence is not None:
        confidence_html = f'<div class="confidence">confidence: {metric.confidence:.0%}</div>'

    findings_html = ""
    if metric.critical_findings or metric.high_findings:
        findings_html = (
            f'<div class="findings">{metric.critical_findings} critical, '
            f"{metric.high_findings} high finding(s)</div>"
        )

    # Plain-English takeaway sits above the fold; the technical wording
    # every agent actually produces (z-scores, confidence floats, agent
    # names) stays exactly as-is but collapsed by default, so someone
    # reading this for the first time isn't met with a wall of jargon,
    # while anyone who wants the full explanation still has it one click
    # away rather than lost entirely.
    return f"""<div class="tile" style="border-left: 4px solid {status_color}">
  <div class="tile-header">
    <span class="tile-label">{_esc(metric.label)}</span>
    {severity_html}
  </div>
  <div class="tile-plain">{_esc(_plain_takeaway(metric))}</div>
  <details class="tile-detail">
    <summary>Show details</summary>
    <div class="tile-headline">{_esc(metric.headline)}</div>
    {confidence_html}
    {findings_html}
    <div class="tile-source">Checked by: {_esc(metric.source_agent)}</div>
  </details>
</div>"""


def _render_freshness_row(entry) -> str:
    status_color = _status_color(entry.outcome == "success")
    detail = _esc(entry.error) if entry.error else f"{entry.row_count} row(s)"
    return (
        f'<div class="source-row" style="border-left: 3px solid {status_color}">'
        f'<span class="source-name">{_esc(entry.dataset)}</span>'
        f'<span class="source-detail">{detail} - pulled {_esc(entry.pulled_at)}</span>'
        f"</div>"
    )


def _fallback_tile(metric: DashboardMetric, exc: Exception) -> str:
    metric_id = getattr(metric, "metric_id", "unknown")
    _log_render_failure("dashboard_tile_render_failed", exc, {"metric_id": metric_id})
    return f'<div class="tile tile-error">Unable to render tile "{_esc(metric_id)}" - see logs.</div>'


def _fallback_page(snapshot: DashboardSnapshot, exc: Exception) -> str:
    dashboard_id = getattr(snapshot, "dashboard_id", "unknown")
    _log_render_failure("dashboard_render_failed", exc, {}, correlation_id=str(dashboard_id))
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Executive Control Tower - Unavailable</title></head>"
        f"<body><h1>Dashboard rendering failed</h1><p>Dashboard {_esc(dashboard_id)} could not be rendered. "
        "See the dashboard service logs for details.</p></body></html>"
    )


def render_dashboard_html(snapshot: DashboardSnapshot) -> str:
    """Render one DashboardSnapshot to a self-contained HTML page.

    Never raises - a rendering failure (a single tile, or page assembly
    itself) is logged and degrades to a fallback card/page instead of
    propagating. See module docstring for the failure paths this covers.
    """
    try:
        tiles_html: list[str] = []
        for metric in snapshot.metrics:
            try:
                tiles_html.append(_render_tile(metric))
            except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
                tiles_html.append(_fallback_tile(metric, exc))

        sources_html: list[str] = []
        for entry in snapshot.data_freshness:
            try:
                sources_html.append(_render_freshness_row(entry))
            except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
                _log_render_failure("dashboard_source_row_render_failed", exc, {"dataset": getattr(entry, "dataset", "unknown")})
                sources_html.append('<div class="source-row tile-error">Unable to render this data source - see logs.</div>')

        sources_section = ""
        if sources_html:
            sources_section = (
                '<div class="sources-section"><h2>Where this data came from</h2>'
                '<div class="sources-intro">Every number above was pulled fresh from these sources when this '
                "page was generated. A source shown in red failed to load, so anything it feeds is missing "
                "above rather than shown wrong.</div>" + "".join(sources_html) + "</div>"
            )

        notification_html = ""
        if snapshot.notification:
            notification_html = f'<div class="notification">{_esc(snapshot.notification)}</div>'

        overall_color = _status_color(snapshot.overall_status == "ok")

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Executive Control Tower</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #f5f6f8; color: #1a1a1a; margin: 0; padding: 24px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  .status {{ display: inline-block; padding: 2px 10px; border-radius: 10px; color: white; font-size: 12px; background: {overall_color}; }}
  .notification {{ background: #fff3cd; border: 1px solid #ffe69c; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }}
  .rollup {{ font-size: 13px; color: #444; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }}
  .tile {{ background: white; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
  .tile-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
  .tile-label {{ font-weight: 600; font-size: 13px; }}
  .badge {{ color: white; font-size: 11px; padding: 1px 8px; border-radius: 8px; text-transform: uppercase; }}
  .tile-headline {{ font-size: 13px; color: #333; margin-bottom: 6px; }}
  .tile-plain {{ font-size: 13px; color: #1a1a1a; margin-bottom: 8px; }}
  .tile-detail {{ margin-top: 4px; }}
  .tile-detail summary {{ cursor: pointer; font-size: 11px; color: #1565c0; }}
  .tile-detail[open] summary {{ margin-bottom: 6px; }}
  .confidence, .findings, .tile-source {{ font-size: 11px; color: #777; }}
  .tile-error {{ color: #c62828; font-size: 12px; }}
  .intro {{ background: white; border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; font-size: 13px; color: #333; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
  .legend {{ margin-top: 8px; font-size: 12px; color: #555; display: flex; flex-wrap: wrap; gap: 14px; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  .sources-section {{ margin-top: 24px; }}
  .sources-section h2 {{ font-size: 14px; margin: 0 0 4px; }}
  .sources-intro {{ font-size: 12px; color: #666; margin-bottom: 8px; }}
  .source-row {{ background: white; border-radius: 4px; padding: 6px 10px; margin-bottom: 4px; font-size: 12px; display: flex; justify-content: space-between; }}
  .source-name {{ font-weight: 600; }}
  .source-detail {{ color: #777; }}
</style>
</head>
<body>
  <h1>Executive Control Tower</h1>
  <div class="meta">dashboard {_esc(snapshot.dashboard_id)} - generated {_esc(snapshot.generated_at)} - <span class="status">{_esc(snapshot.overall_status)}</span></div>
  <div class="intro">
    This page is a plain-English snapshot of your supply chain, rebuilt fresh each time it runs - there is
    nothing to log into and nothing to configure here. Each card below covers one area (demand, stockout risk,
    suppliers, shipments, data quality) with a one-line takeaway; click "Show details" on any card for the full
    technical explanation behind it.
    <div class="legend">
      <span class="legend-item"><span class="legend-dot" style="background:{_STATUS_COLORS['ok']}"></span>Fine / working normally</span>
      <span class="legend-item"><span class="legend-dot" style="background:{_STATUS_COLORS['error']}"></span>Couldn't be checked</span>
      <span class="legend-item"><span class="legend-dot" style="background:{_SEVERITY_COLORS['critical']}"></span>Critical</span>
      <span class="legend-item"><span class="legend-dot" style="background:{_SEVERITY_COLORS['high']}"></span>High</span>
      <span class="legend-item"><span class="legend-dot" style="background:{_SEVERITY_COLORS['medium']}"></span>Medium</span>
      <span class="legend-item"><span class="legend-dot" style="background:{_SEVERITY_COLORS['low']}"></span>Low</span>
    </div>
  </div>
  {notification_html}
  <div class="rollup">{snapshot.total_critical_findings} critical / {snapshot.total_high_findings} high finding(s) across the fleet</div>
  <div class="grid">
    {''.join(tiles_html)}
  </div>
  {sources_section}
</body>
</html>"""
    except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
        return _fallback_page(snapshot, exc)
