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
import json
import re

from local_apps import theme
from dashboard.approval_store import ApprovalRecord
from dashboard.charts import ChartSpec, build_chart_specs, render_bar_chart
from dashboard.logging_setup import get_logger
from dashboard.metrics import DashboardMetric, DashboardSnapshot, KPISummary, compute_kpi_summary

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

# A user asked "it shows Degraded, what does it mean" - the word alone
# (even next to the notification banner explaining *which* sources
# failed) didn't say what the status category itself means. Explains the
# 3 states metrics.py's own DashboardStatus can hold - see
# compute_dashboard_snapshot()'s own overall_status logic there for how
# each is decided.
_STATUS_EXPLANATIONS = {
    "ok": "Every data source and check ran successfully - the results below reflect complete, up-to-date data.",
    "degraded": "Some data sources or checks failed to run - the results below reflect only what did succeed. See the notice above for which ones failed.",
    "error": "Nothing could be checked - no data source succeeded, so there is nothing reliable to show below.",
}

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


# Applied only after _esc() has already escaped the segment - matching and
# wrapping substrings of an already-safe string can never reintroduce
# markup, since neither pattern can match a "<" (there isn't one left) and
# the negative lookbehind keeps _HEADLINE_VALUE_RE from matching digits
# inside an escape entity like "&#x27;" (blocked by the preceding "x").
# The trailing "(?:[-/]\d+)*" keeps a fraction ("65/100") or a hyphenated
# date ("2025-07") as one highlighted token instead of splitting it at the
# "-"/"/" into two separate spans.
_HEADLINE_VALUE_RE = re.compile(r"(?<![\w#/])(-?\$?\d[\d,]*\.?\d*(?:[-/]\d+)*%?)")
_HEADLINE_SEVERITY_RE = re.compile(r"\b(critical|high|medium|low)\b", re.IGNORECASE)

# A user first asked for "confidence"/"risk score" to carry a plain-
# language tooltip (added to their 3 fixed UI locations only); a follow-up
# said other jargon in this same free-form agent prose - "z-score",
# "stockout probability", etc. - "does not explain anything" either.
# GLOSSARY generalizes that into one reusable term list: every occurrence
# of any key (case-insensitive, whole phrase) found in agent-generated
# text gets the same dotted-underline tooltip via _wrap_glossary_terms
# below. Exported (not module-private) so chat_interface/serve_chat_ui.py
# can apply the identical glossary to its own chat answers via
# render_answer_lines() instead of keeping a second copy of these
# definitions in sync by hand.
GLOSSARY: dict[str, str] = {
    "confidence": "How sure the system is in this result, based on how much data was available and how much the underlying checks agreed with each other. Higher is more certain.",
    "risk score": "A single 0-100 score combining how likely a problem is and how severe it would be. Higher means more urgent.",
    "z-score": "A statistical measure of how unusual a value is compared to the recent baseline - the further from 0, the more unusual.",
    "stockout probability": "The estimated chance this item runs out of stock before the next shipment arrives.",
    "safety stock": "The extra buffer of inventory kept on hand in case of unexpected demand or delays.",
    "lead time": "How many days it takes for a reordered shipment to arrive.",
    "on-time rate": "The percentage of past deliveries that arrived by their expected date.",
    "revenue at risk": "The estimated sales value that could be lost if this risk isn't addressed.",
}
# Longest phrase first, so e.g. "risk score" is never partially consumed
# by a shorter, unrelated term matching inside it (none currently overlap,
# but this stays correct as more terms are added).
_GLOSSARY_RE = re.compile(
    "|".join(re.escape(term) for term in sorted(GLOSSARY, key=len, reverse=True)), re.IGNORECASE
)


def _wrap_glossary_terms(escaped_html: str) -> str:
    """Wraps every occurrence of a GLOSSARY term in `escaped_html` with a `.term` tooltip span.

    Must run *after* any other markup (value/severity highlighting) has
    already been inserted into `escaped_html` - none of GLOSSARY's keys
    are substrings of this module's own class names or style attributes,
    so this cannot match inside already-inserted tags, but running it
    first (before those exist) would be the safer order regardless.
    """
    def _wrap(match: re.Match) -> str:
        term = match.group(0)
        definition = GLOSSARY[term.lower()]
        return f'<span class="term" data-tooltip="{html_lib.escape(definition)}">{term}</span>'

    return _GLOSSARY_RE.sub(_wrap, escaped_html)


def _js_string_literal(value: str) -> str:
    """A value safe to embed inside an HTML attribute as a JS string-literal argument.

    Two boundaries stack here: json.dumps() makes it a valid, escaped JS
    string literal (handles quotes/backslashes), and html_lib.escape()
    then makes that literal safe to sit inside a double-quoted HTML
    attribute (e.g. onclick="..."). dashboard_id/metric_id are internal
    identifiers, not typically attacker-controlled, but this module's own
    docstring already treats every agent-derived string as untrusted for
    HTML-injection purposes - same discipline applied here.
    """
    return html_lib.escape(json.dumps(value))


def _esc(value: object) -> str:
    return html_lib.escape(str(value)) if value else ""


def _wrap_value(match: re.Match) -> str:
    # [\d,]* in _HEADLINE_VALUE_RE also happily eats a comma that's really a
    # list separator ("1360, 1360" -> would otherwise highlight "1360,"
    # with the separator comma trapped inside the span) - strip trailing
    # comma(s) back out of the span; a comma in the middle of the digits
    # (a real thousands separator, e.g. "28,006") is left alone.
    value = match.group(1).rstrip(",")
    trailing = match.group(1)[len(value):]
    return f'<span class="hl-value">{value}</span>{trailing}'


def _highlight_headline_segment(escaped_segment: str) -> str:
    """Color-codes numbers/amounts and severity words, and glossary-tags jargon,
    within one already-escaped headline segment, so a reader can pick the
    values that matter - and get a plain-language explanation of the
    technical terms - out of a line of prose without reading every word."""
    highlighted = _HEADLINE_VALUE_RE.sub(_wrap_value, escaped_segment)

    def _color_severity(match: re.Match) -> str:
        word = match.group(1)
        color = _SEVERITY_COLORS.get(word.lower(), _DEFAULT_COLOR)
        return f'<span class="hl-severity" style="color:{color}">{word}</span>'

    highlighted = _HEADLINE_SEVERITY_RE.sub(_color_severity, highlighted)
    return _wrap_glossary_terms(highlighted)


def _render_headline_lines(headline: str) -> str:
    """Splits the (already-condensed) headline back into its list items and
    renders each as its own <li> instead of one run-on sentence, with
    numbers/amounts and severity words color-coded and jargon glossary-
    tagged - see _highlight_headline_segment. Escaping happens once per
    segment, before any highlighting markup is added.

    Regression (2026-09-12): this used to render each segment as its own
    <div> - one fact per line, but not a literal list - which a user
    reading it alongside the AI Assistant's own run-on paragraph answer
    (see render_answer_lines below) called out as "not in a list ...
    check this for all the pages." A real <ul>/<li> now backs it (same
    class names, so no CSS rewrite needed beyond list-style/padding).
    """
    condensed = _condense_headline(headline)
    segments = [s for s in _HEADLINE_LIST_SPLIT_RE.split(condensed) if s]
    if not segments:
        return ""
    items = "".join(f'<li class="headline-line">{_highlight_headline_segment(_esc(s))}</li>' for s in segments)
    return f'<ul class="tile-headline">{items}</ul>'


def render_answer_lines(answer: str) -> str:
    """Renders a free-form agent/chat answer as a real, glossary-tagged <ul> list.

    Public (not module-private) so chat_interface/serve_chat_ui.py can
    call it directly for its own chat bubble's answer text, reusing this
    module's condense/split/highlight/glossary pipeline instead of a
    second copy - added after a user pointed out the AI Assistant's
    answer rendered as one dense run-on paragraph with unexplained jargon
    ("z-score", "stockout probability", ...), while this same pipeline
    already existed for the Live Data screen's own tile headlines.
    """
    return _render_headline_lines(answer)


def _format_currency_stat(value: float | None) -> str:
    """"—" for unknown (no pricing/cost data available), "$0" for a real, computed zero."""
    return "—" if value is None else f"${value:,.0f}"


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


_APPROVAL_BADGE_LABEL = {"approved": "Approved", "rejected": "Rejected"}


def _render_approval_controls(dashboard_id: str, metric_id: str, latest: ApprovalRecord | None) -> str:
    """Approve/Reject buttons plus the current decision badge, if any.

    Buttons always render regardless of `latest` (a rejected or approved
    recommendation can still be re-decided later - see approval_store's
    own "not deduplicated" note). The onclick handler is a single shared
    submitApproval() JS function (defined once in render_dashboard_html's
    page shell) so each tile only needs 2 small buttons, not its own
    inline <script>.
    """
    badge_html = ""
    if latest is not None:
        badge_class = "approval-badge-approved" if latest.decision == "approved" else "approval-badge-rejected"
        label = _APPROVAL_BADGE_LABEL.get(latest.decision, latest.decision)
        badge_html = f'<span class="approval-badge {badge_class}">{_esc(label)} by {_esc(latest.reviewer)}</span>'

    dashboard_id_js = _js_string_literal(dashboard_id)
    metric_id_js = _js_string_literal(metric_id)
    return f"""<div class="approval-row">
    {badge_html}
    <button type="button" class="approval-btn approval-approve" onclick="submitApproval({dashboard_id_js}, {metric_id_js}, 'approved')">Approve</button>
    <button type="button" class="approval-btn approval-reject" onclick="submitApproval({dashboard_id_js}, {metric_id_js}, 'rejected')">Reject</button>
  </div>"""


def _render_approval_history_section(history: list[ApprovalRecord]) -> str:
    """The full "who decided what, when" trail for this dashboard - newest first.

    Empty (renders nothing) when no decision has ever been recorded for
    this dashboard_id, same "don't show an empty section" convention the
    sources/charts sections already follow.
    """
    if not history:
        return ""
    rows = []
    for record in reversed(history):  # newest first
        badge_class = "approval-badge-approved" if record.decision == "approved" else "approval-badge-rejected"
        label = _APPROVAL_BADGE_LABEL.get(record.decision, record.decision)
        note_html = f'<div class="approval-history-note">{_esc(record.note)}</div>' if record.note else ""
        rows.append(
            f'<div class="approval-history-row">'
            f'<span class="approval-badge {badge_class}">{_esc(label)}</span>'
            f'<span class="approval-history-metric">{_esc(record.metric_id)}</span>'
            f'<span class="approval-history-meta">by {_esc(record.reviewer)} &middot; {_esc(record.timestamp)}</span>'
            f"{note_html}"
            f"</div>"
        )
    return (
        '<div class="approval-history-section"><h2>Approval History</h2>'
        '<div class="approval-history-intro">Every human decision recorded against this dashboard\'s '
        "recommendations, most recent first - this is the durable trail behind each Approve/Reject click above."
        "</div>" + "".join(rows) + "</div>"
    )


def _render_tile(metric: DashboardMetric, dashboard_id: str, latest_approval: ApprovalRecord | None) -> str:
    status_color = _STATUS_COLORS.get(metric.status, _DEFAULT_COLOR)

    severity_html = ""
    if metric.severity is not None:
        severity_color = _SEVERITY_COLORS.get(metric.severity, _DEFAULT_COLOR)
        severity_html = f'<span class="badge" style="background:{severity_color}">{_esc(metric.severity)}</span>'

    confidence_html = ""
    if metric.confidence is not None:
        # A user reported "confidence" as a term that "does not explain
        # anything" on its own - a native `title` tooltip (paired with the
        # shared .term CSS's dotted-underline "hover for more" affordance)
        # rather than a permanent inline sentence, so the plain takeaway
        # above stays the primary thing read at a glance.
        confidence_html = (
            '<div class="confidence"><span class="term" data-tooltip="How sure the system is in this result, '
            'based on how much data was available and how much the underlying checks agreed with each '
            f'other. Higher is more certain.">confidence</span>: {metric.confidence:.0%}</div>'
        )

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
    approval_html = _render_approval_controls(dashboard_id, metric.metric_id, latest_approval)
    return f"""<div class="tile" style="border-left: 4px solid {status_color}">
  <div class="tile-header">
    <span class="tile-label">{_esc(metric.label)}</span>
    {severity_html}
  </div>
  <div class="tile-plain">{_esc(_plain_takeaway(metric))}</div>
  <details class="tile-detail">
    <summary>Show details</summary>
    {_render_headline_lines(metric.headline)}
    {confidence_html}
    {findings_html}
    <div class="tile-source">Checked by: {_esc(metric.source_agent)}</div>
  </details>
  {approval_html}
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


def _render_nav(nav_links: list[tuple[str, str | None, str, str]] | None) -> str:
    """A tab bar linking sibling scenario pages together - href=None marks the
    current page (rendered as an inactive, already-here pill instead of a
    link), so opening any one of a set of pages built with the same
    nav_links reads as one application with several views, not several
    unrelated files that happen to sit in the same folder.

    Each entry is (label, href_or_None, icon_svg, accent) - the icon/accent
    pair renders via the same `.nav-icon`/`.nav-icon-{accent}` classes
    local_apps/theme.py's render_nav_bar() uses (2026-09-13, a design-
    review ask for per-tab icons), so this page's own nav bar - built
    separately since it needs to support an arbitrary href-or-None list
    for demo-scenario linking, not just the fixed 5 destinations
    render_nav_bar() enumerates - still looks identical to every other
    screen's."""
    if not nav_links:
        return ""
    items = "".join(
        f'<span class="nav-item nav-current"><span class="nav-icon nav-icon-{accent}">{icon_svg}</span>{_esc(label)}</span>'
        if href is None
        else f'<a class="nav-item" href="{_esc(href)}"><span class="nav-icon nav-icon-{accent}">{icon_svg}</span>{_esc(label)}</a>'
        for label, href, icon_svg, accent in nav_links
    )
    return f'<div class="nav-bar">{items}</div>'


def _render_chart_card(spec: ChartSpec) -> str:
    footnote = f'<div class="chart-footnote">+{spec.omitted_count} more not shown</div>' if spec.omitted_count else ""
    return (
        f'<div class="chart-card"><h3 class="chart-title">{_esc(spec.title)}</h3>'
        f"{render_bar_chart(spec)}{footnote}</div>"
    )


def _render_quick_review_section(charts: list[ChartSpec]) -> str:
    """Always-visible charts a reader can scan in a few seconds - see
    dashboard/charts.py's own module docstring for what qualifies."""
    if not charts:
        return ""
    cards = "".join(_render_chart_card(c) for c in charts)
    return f'<div class="charts-section"><h2>Quick review</h2><div class="charts-grid">{cards}</div></div>'


def _render_explore_section(charts: list[ChartSpec]) -> str:
    """A question-picker over the per-subject charts (SKU/supplier/PO/period
    breakdowns) the fixed tiles/stat-row never show below an aggregate
    count. Pure CSS tab switching (hidden radio inputs + the `:checked`
    general-sibling combinator) - no JavaScript, matching this page's
    existing zero-JS design, and every panel is real markup already in the
    page (nothing rendered by client-side script), so the page still works
    exactly the same whether opened from a server or straight off disk.
    """
    if not charts:
        return ""
    radios: list[str] = []
    tabs: list[str] = []
    panels: list[str] = []
    rules: list[str] = []
    for i, spec in enumerate(charts):
        radio_id = f"explore-{i}"
        panel_id = f"explore-panel-{i}"
        checked = " checked" if i == 0 else ""
        radios.append(f'<input type="radio" name="explore-tab" id="{radio_id}" class="explore-radio"{checked}>')
        tabs.append(f'<label for="{radio_id}" class="explore-tab-label">{_esc(spec.question)}</label>')
        panels.append(f'<div class="explore-panel" id="{panel_id}">{_render_chart_card(spec)}</div>')
        rules.append(
            f"#{radio_id}:checked ~ #{panel_id} {{ display: block; }}"
            f'#{radio_id}:checked ~ .explore-tabs label[for="{radio_id}"] '
            "{ background: var(--brand); color: white; }"
        )
    return (
        '<div class="explore-section"><h2>Explore the data</h2>'
        '<div class="sources-intro">Pick a question to see the chart behind it - covers individual SKUs, '
        "suppliers, purchase orders and time periods the cards above only summarize as a count.</div>"
        f'<div class="explore-control">{"".join(radios)}'
        f'<div class="explore-tabs">{"".join(tabs)}</div>'
        f'{"".join(panels)}</div>'
        f"<style>{''.join(rules)}</style></div>"
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


def render_dashboard_html(
    snapshot: DashboardSnapshot,
    nav_links: list[tuple[str, str | None]] | None = None,
    subtitle: str | None = None,
    latest_approvals: dict[str, ApprovalRecord] | None = None,
    approval_history: list[ApprovalRecord] | None = None,
) -> str:
    """Render one DashboardSnapshot to a self-contained HTML page.

    `nav_links` (optional): (label, href) pairs for the shared tab bar
    linking this page to sibling scenario pages - href=None marks the
    current page. Omitted entirely (the default) for a caller with no
    sibling pages, in which case the page renders exactly as it always
    has, just below this pass's own CSS refresh.

    `subtitle` (optional): one short line under the page title (e.g. which
    scenario this is) - purely cosmetic, never affects the data below it.

    `latest_approvals` (optional): metric_id -> that metric's most recent
    ApprovalRecord (dashboard.approval_store.ApprovalStore.latest_by_metric()'s
    own return shape) - shown as a badge on each tile. `approval_history`
    (optional): every decision ever recorded for this dashboard_id,
    oldest first (ApprovalStore.records_for_dashboard()'s own shape) -
    rendered as the "Approval History" section. Both default to empty
    (no badges, no history section) so a caller with no ApprovalStore
    wired up yet gets exactly the same page as before this feature
    existed - this function stays pure either way, the caller owns
    reading the store.

    Never raises - a rendering failure (a single tile, or page assembly
    itself) is logged and degrades to a fallback card/page instead of
    propagating. See module docstring for the failure paths this covers.
    """
    latest_approvals = latest_approvals or {}
    try:
        tiles_html: list[str] = []
        for metric in snapshot.metrics:
            try:
                tiles_html.append(_render_tile(metric, snapshot.dashboard_id, latest_approvals.get(metric.metric_id)))
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

        try:
            approval_history_section = _render_approval_history_section(approval_history or [])
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            _log_render_failure("dashboard_approval_history_render_failed", exc, {})
            approval_history_section = ""

        # Charts degrade the same way a single broken tile does (see module
        # docstring) - a bug here must not take down the rest of an
        # otherwise-fine page.
        try:
            quick_review_charts, explore_charts = build_chart_specs(snapshot)
            quick_review_html = _render_quick_review_section(quick_review_charts)
            explore_html = _render_explore_section(explore_charts)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            _log_render_failure("dashboard_charts_render_failed", exc, {})
            quick_review_html = ""
            explore_html = ""

        overall_color = _status_color(snapshot.overall_status == "ok")
        subtitle_html = f'<div class="kicker">{_esc(subtitle)}</div>' if subtitle else ""
        header_body_html = (
            f"{subtitle_html}"
            "<h1>Executive Control Tower</h1>"
            f'<div class="meta">dashboard {_esc(snapshot.dashboard_id)} &middot; generated {_esc(snapshot.generated_at)} '
            f'<span class="status">{_esc(snapshot.overall_status)}</span></div>'
        )
        page_header_html = theme.render_page_header(theme.ICON_PULSE, header_body_html, accent="warning")
        nav_html = _render_nav(nav_links)
        try:
            kpis = compute_kpi_summary(snapshot)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            _log_render_failure("dashboard_kpi_render_failed", exc, {})
            kpis = KPISummary(total_revenue_at_risk=None, total_shipment_delay_cost=None)

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Executive Control Tower</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231d4ed8'/%3E%3Cpath d='M8 22V14M15 22V9M22 22V17' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
{theme.GOOGLE_FONT_LINKS}
<style>
{theme.TOKENS_CSS}
  .page {{ max-width: 1180px; margin: 0 auto; padding: 28px 32px 56px; }}
  .kicker {{
    display: inline-block; font-size: 12.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--brand); margin-bottom: 4px;
  }}
  h1 {{ font-size: 27px; font-weight: 700; margin: 0 0 6px; letter-spacing: -0.01em; }}
  .meta {{ color: var(--ink-faint); font-size: 14.5px; margin-bottom: 20px; display: flex; align-items: center; gap: 8px; }}
  .status {{
    display: inline-flex; align-items: center; gap: 5px; padding: 3px 11px; border-radius: 100px; color: white;
    font-size: 12.5px; font-weight: 650; letter-spacing: 0.03em; text-transform: uppercase; background: {overall_color};
  }}
  .notification {{
    background: var(--warning-tint); color: var(--warning); border: 1px solid #f0dcb3; padding: 12px 16px;
    border-radius: 10px; margin-bottom: 20px; font-size: 15px; font-weight: 550;
  }}
  /* Regression (2026-09-12, second report): the previous fix here
     (widening the minimum card width to fight crowding) backfired - a
     fixed set of always-exactly-6 cards under auto-fit wraps onto a
     second line once the row gets narrower than 6 widened cards need, and
     a user asked for these back on one line and smaller. Since there are
     always exactly 6 stat cards (never a variable count), a literal
     6-column grid is the correct tool here, not auto-fit/auto-fill:
     columns always share the row's width equally and shrink together,
     they never wrap - "single line" is guaranteed by the CSS itself, not
     by hoping the viewport is wide enough. */
  .stat-row {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .stat-card {{
    /* A very faint dot-grid, same technique .page-header already uses,
       at half its opacity - a user asked for "a few more background
       pictures" on the app's boxes; kept subtle enough not to compete
       with the number/label it sits behind. */
    background-color: var(--surface);
    background-image: radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.05) 1px, transparent 0);
    background-size: 16px 16px;
    border-radius: 12px; padding: 14px; box-shadow: var(--shadow);
    border-top: 3px solid var(--border); min-width: 0;
    /* A user asked for the page's items to look "more dynamic" - .tile
       below already had this same hover-lift; the KPI boxes never did. */
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }}
  /* Regression (2026-09-12, fourth report): this card used to also set
     overflow: hidden (added for the text-ellipsis pass below, which
     doesn't actually need it - .stat-value/.stat-label already clip
     themselves) - that silently clipped the "Overall status" KPI's own
     .term tooltip, which pops up ABOVE the card and was being cut off by
     the card's own edge. A user reported the tooltip "not visible" with
     no visible CSS error to explain it; overflow: hidden clipping an
     absolutely-positioned popover is exactly that kind of invisible bug. */
  .stat-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19, 24, 38, 0.09); }}
  .stat-card.is-critical {{ border-top-color: var(--danger); }}
  .stat-card.is-high {{ border-top-color: {_SEVERITY_COLORS['high']}; }}
  .stat-card.is-ok {{ border-top-color: var(--success); }}
  .stat-value {{
    font-size: 23px; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .stat-label {{
    font-size: 11.5px; color: var(--ink-faint); font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
    margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 18px; }}
  .tile {{
    background-color: var(--surface);
    background-image: radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.05) 1px, transparent 0);
    background-size: 16px 16px;
    border-radius: 12px; padding: 18px 20px; box-shadow: var(--shadow);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }}
  .tile:hover {{ transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19, 24, 38, 0.09); }}
  .tile-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; gap: 8px; }}
  .tile-label {{ font-weight: 650; font-size: 15.5px; color: var(--ink); }}
  .badge {{
    color: white; font-size: 12px; font-weight: 650; padding: 2px 9px; border-radius: 100px;
    text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap;
  }}
  /* .tile-headline/.headline-line now live in local_apps/theme.py's
     TOKENS_CSS - moved there (2026-09-12) so chat_interface/serve_chat_ui.py
     can render its own chat answers with render_answer_lines() (this same
     module) and get identical list styling, instead of duplicating these
     2 rules into a 3rd screen's <style> block by hand. */
  .hl-value {{ color: var(--brand); font-weight: 650; }}
  .hl-severity {{ font-weight: 650; }}
  .tile-plain {{ font-size: 15px; color: var(--ink); margin-bottom: 10px; font-weight: 550; }}
  .tile-detail {{ margin-top: 4px; border-top: 1px solid var(--border-soft); padding-top: 8px; }}
  .tile-detail summary {{ cursor: pointer; font-size: 13px; color: var(--brand); font-weight: 600; }}
  .tile-detail summary:hover {{ color: var(--brand-dark); }}
  .tile-detail[open] summary {{ margin-bottom: 8px; }}
  .confidence, .findings, .tile-source {{ font-size: 13px; color: var(--ink-faint); margin-top: 3px; }}
  .tile-error {{ color: var(--danger); font-size: 14px; }}
  .intro {{
    background: var(--surface); border-radius: 12px; padding: 18px 22px; margin-bottom: 24px; font-size: 15px;
    color: var(--ink-soft); box-shadow: var(--shadow); border-left: 3px solid var(--brand); line-height: 1.7;
  }}
  .legend {{ margin-top: 16px; font-size: 13px; color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 20px; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
  .sources-section {{ margin-top: 28px; }}
  .sources-section h2 {{ font-size: 16px; font-weight: 700; margin: 0 0 4px; color: var(--ink); }}
  .sources-intro {{ font-size: 13.5px; color: var(--ink-faint); margin-bottom: 10px; }}
  .source-row {{
    background: var(--surface); border-radius: 8px; padding: 9px 14px; margin-bottom: 6px; font-size: 14px;
    display: flex; justify-content: space-between; box-shadow: var(--shadow);
  }}
  .source-name {{ font-weight: 650; color: var(--ink); }}
  .source-detail {{ color: var(--ink-faint); }}
  .charts-section {{ margin-bottom: 22px; }}
  .charts-section h2, .explore-section h2 {{ font-size: 16px; font-weight: 700; margin: 0 0 10px; color: var(--ink); }}
  .charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
  .chart-card {{
    background: var(--surface); border-radius: 12px; padding: 16px 18px 12px; box-shadow: var(--shadow);
    /* Regression (2026-09-12): a lone chart-card - one quick-review chart
       when the other didn't qualify (e.g. 0 critical/high findings), or
       an explore panel, which is always exactly one card at a time - has
       no sibling to share .charts-grid's row with, so its 1fr grid track
       (or, for an explore panel, the full .page width) stretched to fill
       the whole row and its bars/text scaled up along with it. Capped
       here, on every chart-card regardless of how many siblings it has,
       at roughly what one card's width already was in a normal 2-up
       quick-review row. */
    max-width: 560px;
  }}
  .chart-title {{ font-size: 14.5px; font-weight: 650; color: var(--ink); margin: 0 0 10px; }}
  .chart-svg {{ width: 100%; height: auto; display: block; }}
  .chart-label {{ font-size: 12px; fill: var(--ink-soft); }}
  .chart-value {{ font-size: 12px; fill: var(--ink-faint); }}
  .chart-footnote {{ font-size: 12.5px; color: var(--ink-faint); margin-top: 8px; }}
  .chart-empty {{ font-size: 14px; color: var(--ink-faint); }}
  .explore-section {{ margin-top: 28px; }}
  .explore-control > input.explore-radio {{ position: absolute; opacity: 0; pointer-events: none; }}
  .explore-tabs {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }}
  .explore-tab-label {{
    cursor: pointer; font-size: 14px; font-weight: 600; padding: 7px 14px; border-radius: 8px;
    background: var(--neutral-tint); color: var(--ink-soft); border: 1px solid var(--border);
  }}
  .explore-tab-label:hover {{ background: var(--brand-tint); color: var(--brand-dark); }}
  .explore-panel {{ display: none; }}
  .approval-row {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-soft); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .approval-btn {{
    font-size: 12.5px; font-weight: 650; padding: 4px 11px; border-radius: 100px; cursor: pointer;
    border: 1px solid var(--border); background: var(--surface); color: var(--ink-soft);
  }}
  .approval-btn:disabled {{ opacity: 0.5; cursor: default; }}
  .approval-approve:hover {{ background: var(--success-tint); color: var(--success); border-color: var(--success); }}
  .approval-reject:hover {{ background: var(--danger-tint); color: var(--danger); border-color: var(--danger); }}
  .approval-badge {{
    font-size: 12px; font-weight: 650; padding: 2px 9px; border-radius: 100px; text-transform: uppercase; letter-spacing: 0.03em;
  }}
  .approval-badge-approved {{ background: var(--success-tint); color: var(--success); }}
  .approval-badge-rejected {{ background: var(--danger-tint); color: var(--danger); }}
  .approval-history-section {{ margin-top: 28px; }}
  .approval-history-section h2 {{ font-size: 16px; font-weight: 700; margin: 0 0 4px; color: var(--ink); }}
  .approval-history-intro {{ font-size: 13.5px; color: var(--ink-faint); margin-bottom: 10px; }}
  .approval-history-row {{
    background: var(--surface); border-radius: 8px; padding: 9px 14px; margin-bottom: 6px; font-size: 14px;
    box-shadow: var(--shadow); display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .approval-history-metric {{ font-weight: 650; color: var(--ink); }}
  .approval-history-meta {{ color: var(--ink-faint); font-size: 13px; }}
  .approval-history-note {{ flex-basis: 100%; color: var(--ink-soft); font-size: 13.5px; font-style: italic; }}
</style>
</head>
<body>
{theme.ONBOARDING_HTML}
<div class="page">
  {nav_html}
  {page_header_html}
  <div class="intro">
    A clear snapshot of your supply chain, rebuilt fresh each time it runs - nothing to log into, nothing
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
      <div class="stat-value"><span class="term" data-tooltip="{_esc(_STATUS_EXPLANATIONS.get(snapshot.overall_status, ''))}">{_esc(snapshot.overall_status).upper()}</span></div>
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
    <div class="stat-card {'is-critical' if kpis.total_revenue_at_risk else ''}">
      <div class="stat-value">{_format_currency_stat(kpis.total_revenue_at_risk)}</div>
      <div class="stat-label">Revenue at risk</div>
    </div>
    <div class="stat-card {'is-high' if kpis.total_shipment_delay_cost else ''}">
      <div class="stat-value">{_format_currency_stat(kpis.total_shipment_delay_cost)}</div>
      <div class="stat-label">Shipment delay cost</div>
    </div>
  </div>
  {quick_review_html}
  <div class="grid">
    {''.join(tiles_html)}
  </div>
  {explore_html}
  {sources_section}
  {approval_history_section}
</div>
<script>
// Updates the clicked tile's own badge in place rather than reloading the
// page - the static page this ran against would just be re-served
// unchanged (approvals are recorded server-side, not baked back into this
// already-rendered HTML file), so a reload would show nothing new. The
// full Approval History section below only picks up new decisions the
// next time this page is regenerated (Run Analysis, or the next scheduled
// refresh) - a deliberate, documented scope cut, not an oversight.
async function submitApproval(dashboardId, metricId, decision) {{
  const reviewer = prompt('Your name:');
  if (!reviewer) return;
  const note = prompt('Optional note (recorded in the audit trail; shown next time this page regenerates):') || '';
  const btn = event.target;
  const row = btn.closest('.approval-row');
  btn.disabled = true;
  try {{
    const response = await fetch('/api/approve', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{dashboard_id: dashboardId, metric_id: metricId, decision: decision, reviewer: reviewer, note: note}}),
    }});
    const data = await response.json();
    if (!response.ok) {{
      alert('Could not record this decision: ' + (data.error || 'unknown error'));
      btn.disabled = false;
      return;
    }}
    let badge = row.querySelector('.approval-badge');
    if (!badge) {{
      badge = document.createElement('span');
      badge.className = 'approval-badge';
      row.insertBefore(badge, row.firstChild);
    }}
    badge.className = 'approval-badge approval-badge-' + data.decision;
    const label = data.decision === 'approved' ? 'Approved' : 'Rejected';
    badge.textContent = label + ' by ' + data.reviewer;
    btn.disabled = false;
  }} catch (err) {{
    alert('Could not reach the server to record this decision.');
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""
    except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
        return _fallback_page(snapshot, exc)
