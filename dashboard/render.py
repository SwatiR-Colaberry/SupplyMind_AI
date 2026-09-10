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

Visual language matches data_console/serve_data_console.py's token
palette (same --ink/--surface/--brand/... custom properties, same
system-font stack) - this and data_console are both part of the one
SupplyMind AI tool suite, and a shared look is what makes that legible
to someone using both rather than two apps that happen to share a name.
"""

from __future__ import annotations

import html as html_lib
import re

from dashboard.logging_setup import get_logger
from dashboard.metrics import DashboardMetric, DashboardSnapshot

logger = get_logger()

# Muted, deliberately non-neon severity ramp - critical/danger share a
# color (a critical finding *is* the dangerous case), "high" is new (this
# module's own tone, not reused from data_console, since data_console never
# needed a 4-step severity scale), "medium"/"low" reuse the base warning/
# neutral tones data_console already established.
_STATUS_COLORS = {"ok": "#0f7b52", "error": "#9a2b1e"}
_SEVERITY_COLORS = {"critical": "#9a2b1e", "high": "#b5540f", "medium": "#a85c00", "low": "#5f6673"}
_DEFAULT_COLOR = "#5f6673"

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

# An agent's `headline` is one line per finding/row concatenated together
# (see this module's own docstring on why that text is carried through
# unchanged rather than reworded) - on real data that can mean the exact
# same templated sentence repeated hundreds of times, e.g. one line per
# inventory row missing the same field. That isn't "detailed," it's
# unreadable: a 28,000-character wall of identical text behind "Show
# details" fails the same reader this collapsible section exists to
# serve. This condenses the *display* only - metric.headline itself, and
# everything logged/audited from it, is untouched.
_HEADLINE_LIST_SPLIT_RE = re.compile(r"; | \| ")
_HEADLINE_MAX_CHARS = 400
_HEADLINE_MAX_ITEMS = 5


def _condense_headline(text: str) -> str:
    if len(text) <= _HEADLINE_MAX_CHARS:
        return text

    segments = _HEADLINE_LIST_SPLIT_RE.split(text)
    if len(segments) <= 1:
        return text[:_HEADLINE_MAX_CHARS].rstrip() + f"... ({len(text):,} characters total)"

    unique_segments = list(dict.fromkeys(segments))  # de-dupe exact repeats, keep first-seen order
    shown = unique_segments[:_HEADLINE_MAX_ITEMS]
    hidden = len(segments) - len(shown)
    condensed = "; ".join(shown)
    if hidden > 0:
        condensed += f" (+{hidden} more, {len(unique_segments)} unique of {len(segments)} total)"
    return condensed


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
    <div class="tile-headline">{_esc(_condense_headline(metric.headline))}</div>
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


def _render_nav(nav_links: list[tuple[str, str | None]] | None) -> str:
    """A tab bar linking sibling scenario pages together - href=None marks the
    current page (rendered as an inactive, already-here pill instead of a
    link), so opening any one of a set of pages built with the same
    nav_links reads as one application with several views, not several
    unrelated files that happen to sit in the same folder."""
    if not nav_links:
        return ""
    items = "".join(
        f'<span class="nav-item nav-current">{_esc(label)}</span>'
        if href is None
        else f'<a class="nav-item" href="{_esc(href)}">{_esc(label)}</a>'
        for label, href in nav_links
    )
    return f'<div class="nav-bar">{items}</div>'


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


def render_dashboard_html(
    snapshot: DashboardSnapshot,
    nav_links: list[tuple[str, str | None]] | None = None,
    subtitle: str | None = None,
) -> str:
    """Render one DashboardSnapshot to a self-contained HTML page.

    `nav_links` (optional): (label, href) pairs for the shared tab bar
    linking this page to sibling scenario pages - href=None marks the
    current page. Omitted entirely (the default) for a caller with no
    sibling pages, in which case the page renders exactly as it always
    has, just below this pass's own CSS refresh.

    `subtitle` (optional): one short line under the page title (e.g. which
    scenario this is) - purely cosmetic, never affects the data below it.

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
        subtitle_html = f'<div class="kicker">{_esc(subtitle)}</div>' if subtitle else ""
        nav_html = _render_nav(nav_links)

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Executive Control Tower</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231d4ed8'/%3E%3Cpath d='M8 22V14M15 22V9M22 22V17' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
  :root {{
    --ink: #131826; --ink-soft: #4a5468; --ink-faint: #8891a1;
    --surface: #ffffff; --canvas: #f2f4f8; --border: #e1e5ec; --border-soft: #edeff3;
    --brand: #1d4ed8; --brand-dark: #1638a6; --brand-tint: #eaf0fd;
    --success: #0f7b52; --success-tint: #e6f4ec;
    --warning: #a85c00; --warning-tint: #fbf0dd;
    --neutral: #5f6673; --neutral-tint: #eef0f3;
    --danger: #9a2b1e; --danger-tint: #fdecea;
    --shadow: 0 1px 2px rgba(19, 24, 38, 0.05), 0 1px 8px rgba(19, 24, 38, 0.04);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--canvas); color: var(--ink); margin: 0; -webkit-font-smoothing: antialiased;
  }}
  .page {{ max-width: 1180px; margin: 0 auto; padding: 28px 32px 56px; }}
  .nav-bar {{
    display: flex; gap: 6px; margin-bottom: 20px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 5px; box-shadow: var(--shadow); width: fit-content;
  }}
  .nav-item {{
    font-size: 12.5px; font-weight: 600; padding: 7px 14px; border-radius: 7px; text-decoration: none;
    color: var(--ink-soft);
  }}
  a.nav-item:hover {{ background: var(--brand-tint); color: var(--brand-dark); }}
  .nav-current {{ background: var(--brand); color: white; }}
  .kicker {{
    display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--brand); margin-bottom: 4px;
  }}
  h1 {{ font-size: 26px; font-weight: 700; margin: 0 0 6px; letter-spacing: -0.01em; }}
  .meta {{ color: var(--ink-faint); font-size: 13px; margin-bottom: 20px; display: flex; align-items: center; gap: 8px; }}
  .status {{
    display: inline-flex; align-items: center; gap: 5px; padding: 3px 11px; border-radius: 100px; color: white;
    font-size: 11px; font-weight: 650; letter-spacing: 0.03em; text-transform: uppercase; background: {overall_color};
  }}
  .notification {{
    background: var(--warning-tint); color: var(--warning); border: 1px solid #f0dcb3; padding: 12px 16px;
    border-radius: 10px; margin-bottom: 20px; font-size: 13.5px; font-weight: 550;
  }}
  .stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 22px; }}
  .stat-card {{
    background: var(--surface); border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow);
    border-top: 3px solid var(--border);
  }}
  .stat-card.is-critical {{ border-top-color: var(--danger); }}
  .stat-card.is-high {{ border-top-color: {_SEVERITY_COLORS['high']}; }}
  .stat-card.is-ok {{ border-top-color: var(--success); }}
  .stat-value {{ font-size: 28px; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1; }}
  .stat-label {{ font-size: 11.5px; color: var(--ink-faint); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 4px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 14px; }}
  .tile {{
    background: var(--surface); border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }}
  .tile:hover {{ transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19, 24, 38, 0.09); }}
  .tile-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 8px; }}
  .tile-label {{ font-weight: 650; font-size: 14px; color: var(--ink); }}
  .badge {{
    color: white; font-size: 10.5px; font-weight: 650; padding: 2px 9px; border-radius: 100px;
    text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap;
  }}
  .tile-headline {{ font-size: 12.5px; color: var(--ink-soft); margin-bottom: 6px; line-height: 1.5; }}
  .tile-plain {{ font-size: 13.5px; color: var(--ink); margin-bottom: 10px; font-weight: 550; }}
  .tile-detail {{ margin-top: 4px; border-top: 1px solid var(--border-soft); padding-top: 8px; }}
  .tile-detail summary {{ cursor: pointer; font-size: 11.5px; color: var(--brand); font-weight: 600; }}
  .tile-detail summary:hover {{ color: var(--brand-dark); }}
  .tile-detail[open] summary {{ margin-bottom: 8px; }}
  .confidence, .findings, .tile-source {{ font-size: 11.5px; color: var(--ink-faint); margin-top: 3px; }}
  .tile-error {{ color: var(--danger); font-size: 12.5px; }}
  .intro {{
    background: var(--surface); border-radius: 12px; padding: 16px 18px; margin-bottom: 20px; font-size: 13.5px;
    color: var(--ink-soft); box-shadow: var(--shadow); border-left: 3px solid var(--brand); line-height: 1.6;
  }}
  .legend {{ margin-top: 12px; font-size: 11.5px; color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
  .sources-section {{ margin-top: 28px; }}
  .sources-section h2 {{ font-size: 15px; font-weight: 700; margin: 0 0 4px; color: var(--ink); }}
  .sources-intro {{ font-size: 12px; color: var(--ink-faint); margin-bottom: 10px; }}
  .source-row {{
    background: var(--surface); border-radius: 8px; padding: 9px 14px; margin-bottom: 6px; font-size: 12.5px;
    display: flex; justify-content: space-between; box-shadow: var(--shadow);
  }}
  .source-name {{ font-weight: 650; color: var(--ink); }}
  .source-detail {{ color: var(--ink-faint); }}
</style>
</head>
<body>
<div class="page">
  {nav_html}
  {subtitle_html}
  <h1>Executive Control Tower</h1>
  <div class="meta">dashboard {_esc(snapshot.dashboard_id)} &middot; generated {_esc(snapshot.generated_at)} <span class="status">{_esc(snapshot.overall_status)}</span></div>
  <div class="intro">
    A plain-English snapshot of your supply chain, rebuilt fresh each time it runs - nothing to log into, nothing
    to configure here. Each card below covers one area (demand, stockout risk, suppliers, shipments, data
    quality) with a one-line takeaway; click "Show details" on any card for the full technical explanation
    behind it.
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
  <div class="stat-row">
    <div class="stat-card {'is-ok' if snapshot.overall_status == 'ok' else 'is-high'}">
      <div class="stat-value">{_esc(snapshot.overall_status).upper()}</div>
      <div class="stat-label">Overall status</div>
    </div>
    <div class="stat-card {'is-critical' if snapshot.total_critical_findings else ''}">
      <div class="stat-value">{snapshot.total_critical_findings}</div>
      <div class="stat-label">Critical findings</div>
    </div>
    <div class="stat-card {'is-high' if snapshot.total_high_findings else ''}">
      <div class="stat-value">{snapshot.total_high_findings}</div>
      <div class="stat-label">High findings</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{len(snapshot.metrics)}</div>
      <div class="stat-label">Areas monitored</div>
    </div>
  </div>
  <div class="grid">
    {''.join(tiles_html)}
  </div>
  {sources_section}
</div>
</body>
</html>"""
    except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
        return _fallback_page(snapshot, exc)
