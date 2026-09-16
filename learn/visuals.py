"""Small, self-contained SVG "explainer" visuals for the Learn page.

Pure functions, no I/O - given the same numbers, always produce the same
SVG string, same discipline dashboard/charts.py's own module docstring
states for its bar charts. Deliberately not shared with
dashboard/charts.py: these are teaching visuals with example numbers
baked in for a fixed set of glossary terms, not live-data charts driven
by a DashboardSnapshot - a different responsibility, so a separate
module rather than growing charts.py to serve two callers with two
different contracts.

4 visual shapes cover all 8 of dashboard/render.py:GLOSSARY's terms:
- render_meter_svg: a single 0-100 fill bar (confidence, risk score,
  stockout probability, on-time rate - anything naturally a percentage
  or a 0-100 score).
- render_timeline_svg: an "order placed -> arrives" line (lead time).
- render_split_bar_svg: a 2-segment bar showing a "normal"/"buffer" (or
  "safe"/"at risk") split (safety stock, revenue at risk).
- render_scale_svg: a signed number line with a shaded "typical" band and
  a marked point (z-score).

Regression (2026-09-13): every function used to also draw its own
caption as an SVG <text> element, sized in the 220-unit viewBox's own
coordinate space. Longer captions (e.g. "72 out of 100 - solid, but
worth a second look") ran past the viewBox's edges - a default root
<svg>'s overflow is 'hidden' per the UA stylesheet, so those captions
were silently clipped on both ends, a user reported as "some values are
getting cut." Fixed by dropping every caption <text> element from these
functions entirely - callers now render the caption as a normal HTML
element next to the SVG (see learn/serve_learn.py's _render_card()),
which can never overflow a fixed coordinate space because it isn't one.
Each function still takes an `elem_id` so callers can wire a live
slider to it (see learn/serve_learn.py's ONBOARDING-style inline script)
by updating specific sub-elements' attributes directly, without
re-generating and re-parsing a whole new SVG string per input event.
"""

from __future__ import annotations

import html as html_lib

_VIEWBOX_WIDTH = 220.0


def _esc(value: object) -> str:
    return html_lib.escape(str(value))


def render_meter_svg(value: float, elem_id: str, color: str = "#1d4ed8") -> str:
    """A single 0-100 fill bar. `value` is clamped to [0, 100]. `elem_id` names the fill <rect> for JS to resize live."""
    clamped = max(0.0, min(100.0, value))
    fill_width = 2.0 + (clamped / 100.0) * (_VIEWBOX_WIDTH - 4.0)
    return (
        f'<svg viewBox="0 0 {_VIEWBOX_WIDTH:.0f} 24" class="explainer-svg" role="img" '
        f'aria-label="{clamped:.0f} out of 100">'
        f'<rect x="0" y="3" width="{_VIEWBOX_WIDTH:.0f}" height="18" rx="9" fill="var(--canvas)" stroke="var(--border)"></rect>'
        f'<rect id="{_esc(elem_id)}-fill" x="0" y="3" width="{fill_width:.1f}" height="18" rx="9" fill="{_esc(color)}"></rect>'
        "</svg>"
    )


def render_timeline_svg(elem_id: str) -> str:
    """An "order placed -> arrives" timeline. Fixed 2 endpoints - lead time's slider only ever changes the caption text beside this, not this visual's own shape."""
    return (
        f'<svg viewBox="0 0 {_VIEWBOX_WIDTH:.0f} 40" class="explainer-svg" role="img" '
        f'aria-label="Order placed to arrival timeline" id="{_esc(elem_id)}">'
        '<line x1="18" y1="28" x2="202" y2="28" stroke="var(--border)" stroke-width="3"></line>'
        '<circle cx="18" cy="28" r="7" fill="var(--brand)"></circle>'
        '<circle cx="202" cy="28" r="7" fill="var(--success)"></circle>'
        '<text x="18" y="14" text-anchor="middle" class="explainer-tick">Order placed</text>'
        '<text x="202" y="14" text-anchor="middle" class="explainer-tick">Arrives</text>'
        "</svg>"
    )


def render_split_bar_svg(left_label: str, left_pct: float, right_label: str, elem_id: str, right_color: str = "#a85c00") -> str:
    """A 2-segment horizontal bar. `left_pct` (0-100) is the left (normal/safe) segment's share. `elem_id` names the 2 rects/labels for JS to resize live."""
    clamped = max(0.0, min(100.0, left_pct))
    left_width = (clamped / 100.0) * _VIEWBOX_WIDTH
    right_width = _VIEWBOX_WIDTH - left_width
    left_text_x = max(left_width / 2, 4.0)
    right_text_x = left_width + max(right_width / 2, 4.0)
    return (
        f'<svg viewBox="0 0 {_VIEWBOX_WIDTH:.0f} 24" class="explainer-svg" role="img" '
        f'aria-label="{_esc(left_label)} vs {_esc(right_label)}">'
        f'<rect id="{_esc(elem_id)}-left" x="0" y="3" width="{left_width:.1f}" height="18" rx="4" '
        'fill="var(--brand-tint)" stroke="var(--border)"></rect>'
        f'<rect id="{_esc(elem_id)}-right" x="{left_width:.1f}" y="3" width="{right_width:.1f}" height="18" rx="4" '
        f'fill="{_esc(right_color)}"></rect>'
        f'<text id="{_esc(elem_id)}-left-label" x="{left_text_x:.1f}" y="17" text-anchor="middle" '
        f'class="explainer-tick">{_esc(left_label)}</text>'
        f'<text id="{_esc(elem_id)}-right-label" x="{right_text_x:.1f}" y="17" text-anchor="middle" '
        f'class="explainer-tick" fill="white">{_esc(right_label)}</text>'
        "</svg>"
    )


def render_scale_svg(value: float, elem_id: str, low: float = -3.0, high: float = 3.0) -> str:
    """A signed number line from `low` to `high` with a shaded 'typical' middle band (-2..2) and a marker at `value`. `elem_id` names the marker <circle> for JS to reposition live."""
    span = high - low

    def x_for(v: float) -> float:
        return 4.0 + ((v - low) / span) * (_VIEWBOX_WIDTH - 8.0)

    typical_low_x = x_for(max(low, -2.0))
    typical_high_x = x_for(min(high, 2.0))
    marker_x = x_for(max(low, min(high, value)))
    return (
        f'<svg viewBox="0 0 {_VIEWBOX_WIDTH:.0f} 24" class="explainer-svg" role="img" '
        f'aria-label="{value:g}, typical range is -2 to 2">'
        '<line x1="4" y1="12" x2="216" y2="12" stroke="var(--border)" stroke-width="3"></line>'
        f'<rect x="{typical_low_x:.1f}" y="6" width="{(typical_high_x - typical_low_x):.1f}" height="12" '
        'fill="var(--brand-tint)"></rect>'
        f'<circle id="{_esc(elem_id)}-marker" cx="{marker_x:.1f}" cy="12" r="6" fill="var(--warning-strong)"></circle>'
        "</svg>"
    )
