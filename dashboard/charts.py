"""Chart generation for the Executive Control Tower (STORY-009 follow-on).

Two small, deliberately-paired responsibilities: deciding *what* to chart
from a DashboardSnapshot (`build_chart_specs`), and drawing a ChartSpec as
inline SVG (`render_bar_chart`). No external charting library and no
network request at render time - this repo's dashboard pages are meant to
open standalone (no CDN, no build step; see dashboard/render.py's own
module docstring on the same constraint for its CSS), and a horizontal
bar chart is simple enough to draw by hand with plain SVG shapes.

Pure and deterministic throughout - same DashboardSnapshot always produces
the same chart specs and the same SVG string; no I/O, no randomness, no
clock reads.

Two families of chart come out of `build_chart_specs`:

- A fixed "quick review" pair, always first when there's data for them:
  findings-by-area and confidence-by-area, both readable straight off the
  DashboardSnapshot's own per-metric severity counts and confidence - the
  same numbers the KPI stat row and tiles already show, just broken out
  per area instead of totalled.
- One "explore" chart per (agent, subject_kind) pair that has findings -
  e.g. "Which SKUs need attention in Stockout Risk?" - built from
  DashboardMetric.findings, which the tiles/stat-row never break down
  below an aggregate count. This is deliberately grouped by subject_kind
  rather than hardcoded per agent name, matching dashboard/metrics.py's
  own stated constraint ("must not depend on any one agent's identity")
  - a future agent with per-subject findings gets an explore chart for
  free, no change needed here.
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass, field

from agents.contracts import FindingSeverity
from dashboard.metrics import DashboardSnapshot

# Same values as dashboard/render.py's _SEVERITY_COLORS/_STATUS_COLORS -
# duplicated rather than imported. render.py already renders charts.py's
# output (embeds the SVG strings this module returns), so the reverse
# import here would be the same "A imports B imports A" cycle this
# session's other entries already name and avoid the same way (see
# forecasting/aggregation.py's _FALLBACK_DATE_FORMATS, data_console's
# _DASHBOARD_SCENARIO_LABELS).
_SEVERITY_COLORS: dict[str, str] = {"critical": "#9a2b1e", "high": "#b5540f", "medium": "#a85c00", "low": "#5f6673"}
_BRAND_COLOR = "#1d4ed8"
_DEFAULT_COLOR = "#5f6673"

_SEVERITY_RANK: dict[FindingSeverity, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}

_SUBJECT_KIND_PLURAL: dict[str, str] = {
    "sku": "SKUs",
    "po": "purchase orders",
    "period": "time periods",
    "supplier": "suppliers",
    "category": "categories",
    "region": "regions",
}

# Singular form for each plural above - kept as an explicit pair rather
# than derived by stripping a trailing "s" off _SUBJECT_KIND_PLURAL
# (which was fine for every kind added before "category": stripping
# "categories" that way gives "categorie", not "category").
_SUBJECT_KIND_SINGULAR: dict[str, str] = {
    "sku": "SKU",
    "po": "purchase order",
    "period": "time period",
    "supplier": "supplier",
    "category": "category",
    "region": "region",
}

_MAX_BARS_PER_CHART = 12


@dataclass(frozen=True)
class ChartBar:
    label: str
    value: float
    color: str


@dataclass(frozen=True)
class ChartSpec:
    chart_id: str  # stable, HTML-id-safe - used for the explore section's radio/anchor wiring
    title: str
    question: str  # the plain-English question this chart answers, shown as the tab/option label
    bars: list[ChartBar] = field(default_factory=list)
    value_suffix: str = ""
    omitted_count: int = 0  # how many additional subjects exist beyond what `bars` shows


def _esc(value: object) -> str:
    return html_lib.escape(str(value)) if value else ""


def _findings_by_area_chart(snapshot: DashboardSnapshot) -> ChartSpec | None:
    bars = [
        ChartBar(
            label=m.label,
            value=m.critical_findings + m.high_findings,
            color=_SEVERITY_COLORS.get(m.severity or "", _DEFAULT_COLOR),
        )
        for m in snapshot.metrics
        if m.status == "ok" and (m.critical_findings + m.high_findings) > 0
    ]
    if not bars:
        return None
    bars.sort(key=lambda b: b.value, reverse=True)
    return ChartSpec(
        chart_id="findings-by-area",
        title="Findings by area",
        question="Where are the critical/high findings concentrated?",
        bars=bars,
    )


def _confidence_by_area_chart(snapshot: DashboardSnapshot) -> ChartSpec | None:
    bars = [
        ChartBar(label=m.label, value=round(m.confidence * 100), color=_BRAND_COLOR)
        for m in snapshot.metrics
        if m.status == "ok" and m.confidence is not None
    ]
    if not bars:
        return None
    bars.sort(key=lambda b: b.value, reverse=True)
    return ChartSpec(
        chart_id="confidence-by-area",
        title="Confidence by area",
        question="How confident is each area in what it's reporting?",
        bars=bars,
        value_suffix="%",
    )


def _explore_charts(snapshot: DashboardSnapshot) -> list[ChartSpec]:
    specs: list[ChartSpec] = []
    for metric in snapshot.metrics:
        if metric.status != "ok" or not metric.findings:
            continue
        by_kind: dict[str, list] = {}
        for finding in metric.findings:
            by_kind.setdefault(finding.subject_kind, []).append(finding)
        for kind, findings in by_kind.items():
            ordered = sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.subject), reverse=True)
            shown = ordered[:_MAX_BARS_PER_CHART]
            bars = [
                ChartBar(
                    label=f.subject,
                    value=_SEVERITY_RANK[f.severity],
                    color=_SEVERITY_COLORS.get(f.severity, _DEFAULT_COLOR),
                )
                for f in shown
            ]
            plural = _SUBJECT_KIND_PLURAL.get(kind, f"{kind}s")
            singular = _SUBJECT_KIND_SINGULAR.get(kind, kind)
            specs.append(
                ChartSpec(
                    chart_id=f"{metric.metric_id}-{kind}",
                    title=f"{metric.label} by {singular}",
                    question=f"Which {plural} need attention in {metric.label}?",
                    bars=bars,
                    omitted_count=max(0, len(ordered) - len(shown)),
                )
            )
    return specs


def build_chart_specs(snapshot: DashboardSnapshot) -> tuple[list[ChartSpec], list[ChartSpec]]:
    """Returns (quick_review_charts, explore_charts) for one DashboardSnapshot.

    Both lists can be empty (e.g. every agent failed) - callers should
    treat that as "nothing to chart yet", not an error.
    """
    quick_review = [c for c in (_findings_by_area_chart(snapshot), _confidence_by_area_chart(snapshot)) if c]
    return quick_review, _explore_charts(snapshot)


# --- SVG rendering -----------------------------------------------------

_BAR_HEIGHT = 22
_BAR_GAP = 10
_ROW_HEIGHT = _BAR_HEIGHT + _BAR_GAP
_LABEL_WIDTH = 150
_CHART_WIDTH = 520
_VALUE_GUTTER = 50
_BAR_AREA_WIDTH = _CHART_WIDTH - _LABEL_WIDTH - _VALUE_GUTTER
_MAX_LABEL_CHARS = 22


def _truncate_label(label: str) -> str:
    return label if len(label) <= _MAX_LABEL_CHARS else label[: _MAX_LABEL_CHARS - 1] + "…"


def render_bar_chart(spec: ChartSpec) -> str:
    """Render one ChartSpec as a self-contained inline SVG horizontal bar chart.

    Deliberately minimal - a title (rendered by the caller, not here),
    bars, end labels, no gridlines/axes/legend - matching the "Bloomberg
    meets Salesforce" target look this repo's CLAUDE.md names rather than
    aiming to be a general-purpose charting engine.
    """
    if not spec.bars:
        return '<div class="chart-empty">Nothing to chart yet.</div>'

    max_value = max((b.value for b in spec.bars), default=0) or 1.0
    height = len(spec.bars) * _ROW_HEIGHT
    rows: list[str] = []
    for i, bar in enumerate(spec.bars):
        y = i * _ROW_HEIGHT
        bar_width = (bar.value / max_value) * _BAR_AREA_WIDTH if bar.value > 0 else 0.0
        bar_width = max(bar_width, 2.0)
        label = _esc(_truncate_label(bar.label))
        value_text = _esc(f"{bar.value:g}{spec.value_suffix}")
        text_y = y + _BAR_HEIGHT * 0.68
        rows.append(
            f'<text x="{_LABEL_WIDTH - 8}" y="{text_y:.1f}" text-anchor="end" class="chart-label">{label}</text>'
            f'<rect x="{_LABEL_WIDTH}" y="{y}" width="{bar_width:.1f}" height="{_BAR_HEIGHT}" rx="3" '
            f'fill="{bar.color}"></rect>'
            f'<text x="{_LABEL_WIDTH + bar_width + 6:.1f}" y="{text_y:.1f}" class="chart-value">{value_text}</text>'
        )
    return (
        f'<svg viewBox="0 0 {_CHART_WIDTH} {height}" class="chart-svg" role="img" '
        f'aria-label="{_esc(spec.title)}">{"".join(rows)}</svg>'
    )
