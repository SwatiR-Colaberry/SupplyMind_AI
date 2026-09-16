"""Local browser "Glossary" screen - the deep-dive companion to the quick .term hover tooltips.

Before this module existed, every jargon explanation in the app lived in
one of two places: an inline hover tooltip (local_apps/theme.py's .term,
one sentence, easy to miss and easy to lose once the mouse moves away) or
a compact glossary panel (local_apps/theme.py's render_glossary_panel(),
a one-line dt/dd pair per term, added to all 4 other screens). A user
asked for something with more room: "a different page where it is
explained in more details but in beginner language with some charts and
images," after separately pointing out the glossary panel was "only
accessible in [the] what if tab." This module is that page - one card
per dashboard/render.py:GLOSSARY term, a few sentences of plain-language
explanation, and a small SVG visual (see learn/visuals.py) instead of a
bare number.

Named "Glossary" in the nav bar and this page's own <h1>/<title> (not
"Learn," its original label) after a follow-up asked for "something more
meaningful" - this page's content genuinely is a glossary, so the label
says exactly that. The module/package/URL prefix are still named `learn`
(internal identifiers no user ever sees - see local_apps/urls.py's
LEARN_URL) to avoid an otherwise-pointless file rename. That same
follow-up also pointed out the per-page glossary panels hadn't actually
been removed from the other 4 screens once this page existed - they have
been now (see local_apps/theme.py's render_glossary_panel() callers'
own git history); the inline .term hover tooltips stay everywhere, this
page is now the one place the fuller explanation lives.

Every visual here started as a static picture with a hardcoded example
number baked into its own caption text. 2 problems, both from the same
follow-up round: (1) longer captions (e.g. "72 out of 100 - solid, but
worth a second look") overflowed the SVG's own 220-unit viewBox and were
silently clipped - a default root <svg>'s overflow is 'hidden', so text
extending past the edges just disappears, no error, nothing to see in
devtools unless you know to look for it. Fixed by moving every caption
out of the SVG into a normal HTML element (see learn/visuals.py's own
module docstring for the details) - normal text can't overflow a fixed
coordinate space because it isn't one. (2) a request for "some dynamic
ideas" - each card now has an interactive slider (<input type="range">)
that live-updates its own visual and caption via the inline <script> at
the bottom of this page (a JS mirror of this module's own caption-band
functions below, same acknowledged JS/Python duplication tradeoff this
app already carries elsewhere for tooltip wording) - not just a static
picture, something to actually play with while reading the explanation.

Deliberately static server-side: every term/explanation/example number
here is a fixed constant, not driven by any live snapshot or user input,
so - unlike chat_interface/serve_chat_ui.py or
scenario_simulation/serve_scenario_simulator.py - there is no POST route
and no request body to validate; the sliders' interactivity is entirely
client-side JS with no server round-trip. Same minimal, dependency-free
stdlib http.server approach as the other 4 screens, for the same reason:
no new package, no new moving part beyond what this repo already runs.

Dev-only: no auth, no TLS, single-threaded, binds to 127.0.0.1 only, and
exits if the port is already taken rather than silently reusing whatever
is already listening there.

Usage:
    python3 -m learn.serve_learn
    # then open http://127.0.0.1:8768 in a browser
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template

from local_apps import http_helpers, theme
from learn import visuals

DEFAULT_PORT = 8768

# Card accent colors reuse the existing token palette (no new hues) -
# also each meter/scale visual's own fill/marker color, so the card's
# top border agrees with the picture inside it instead of being an
# arbitrary 3rd color.
_BRAND = "#1d4ed8"
_WARNING = "#a85c00"
_SUCCESS = "#0f7b52"
_DANGER = "#9a2b1e"


@dataclass(frozen=True)
class TermExplainer:
    term: str
    explanation: str
    slug: str  # HTML id prefix for this card's SVG/caption/slider - must be unique per card
    visual_type: str  # "meter" | "timeline" | "splitbar" | "scale"
    accent: str
    # meter: 0-100 value, direction "positive" (higher=better) or "negative" (higher=worse)
    value: float = 0.0
    direction: str = "positive"
    # timeline: whole days
    days: float = 10.0
    # splitbar: left segment's share (0-100) plus both segments' labels
    left_label: str = ""
    left_pct: float = 0.0
    right_label: str = ""
    # scale: signed value, -3..3
    scale_value: float = 0.0


# Wording deliberately more conversational than dashboard/render.py's own
# GLOSSARY one-liners (this page's whole purpose is more room, in
# beginner language) - not pulled from that dict directly, though every
# term name here matches one of its keys, so a reader who hovers a .term
# tooltip elsewhere in the app and then visits this page finds the same
# word explained further, not a differently-named concept.
_TERM_EXPLAINERS: list[TermExplainer] = [
    TermExplainer(
        term="Confidence",
        explanation=(
            "How sure the system is in a result - not a guess pulled out of thin air, but a number built from "
            "how much data was available and how closely different automatic checks agreed with each other. "
            "If two checks looked at the same problem and reached very different conclusions, confidence goes "
            "down, even if each check was sure of its own answer."
        ),
        slug="confidence",
        visual_type="meter",
        accent=_BRAND,
        value=72,
        direction="positive",
    ),
    TermExplainer(
        term="Risk score",
        explanation=(
            "A single number from 0 to 100 that combines two things: how likely a problem is, and how bad it "
            "would be if it happened. A problem that's very likely but only mildly inconvenient can score lower "
            "than one that's less likely but would shut down a warehouse."
        ),
        slug="risk-score",
        visual_type="meter",
        accent=_WARNING,
        value=34,
        direction="negative",
    ),
    TermExplainer(
        term="Z-score",
        explanation=(
            "A way of asking \"is this normal, or unusual?\" It measures how far a number is from what's "
            "typically seen, counted in steps called standard deviations. Near 0 means normal. Past about 2, "
            "it's worth a second look; past 3, it's rare enough to investigate."
        ),
        slug="z-score",
        visual_type="scale",
        accent=_WARNING,
        scale_value=2.19,
    ),
    TermExplainer(
        term="Stockout probability",
        explanation=(
            "The estimated chance a specific item runs completely out of stock before its next shipment "
            "arrives. It's a probability, not a certainty - an item at 8% will usually be fine, but it isn't "
            "risk-free."
        ),
        slug="stockout-probability",
        visual_type="meter",
        accent=_SUCCESS,
        value=8,
        direction="negative",
    ),
    TermExplainer(
        term="Safety stock",
        explanation=(
            "Extra inventory kept on hand above what you expect to sell, specifically to absorb the "
            "unexpected - a supplier running late, a sudden spike in orders. Think of it as a cushion, not "
            "part of your regular selling stock."
        ),
        slug="safety-stock",
        visual_type="splitbar",
        accent=_WARNING,
        left_label="Everyday stock",
        left_pct=80,
        right_label="Safety buffer",
    ),
    TermExplainer(
        term="Lead time",
        explanation=(
            "The number of days between placing a reorder and that shipment actually arriving on your shelf. "
            "The longer the lead time, the earlier you need to reorder - and the more safety stock you "
            "typically need to cover the gap while you wait."
        ),
        slug="lead-time",
        visual_type="timeline",
        accent=_BRAND,
        days=10,
    ),
    TermExplainer(
        term="On-time rate",
        explanation=(
            "The percentage of past deliveries that arrived by the date they were promised. A supplier at "
            "94% misses their promised date roughly 1 time in 17 - useful for deciding how much of a buffer "
            "to plan around them."
        ),
        slug="on-time-rate",
        visual_type="meter",
        accent=_SUCCESS,
        value=94,
        direction="positive",
    ),
    TermExplainer(
        term="Revenue at risk",
        explanation=(
            "The estimated dollar value of sales that could be lost if a risk isn't addressed - calculated "
            "from the units likely to fall short and what each one sells for. It turns \"we might run low\" "
            "into a number a real business decision can be made against."
        ),
        slug="revenue-at-risk",
        visual_type="splitbar",
        accent=_DANGER,
        left_label="Protected revenue",
        left_pct=75,
        right_label="At risk",
    ),
]


def _meter_band(value: float, direction: str) -> str:
    """Matches the JS `meterBand()` mirror in _ONBOARDING-adjacent <script> below - kept in sync by hand."""
    if direction == "positive":
        if value >= 70:
            return "strong"
        if value >= 40:
            return "moderate"
        return "low"
    if value >= 50:
        return "high"
    if value >= 20:
        return "moderate"
    return "low"


def _meter_caption(value: float, direction: str) -> str:
    return f"{value:.0f} out of 100 - {_meter_band(value, direction)}"


def _scale_caption(value: float) -> str:
    band = "outside the typical range, worth a look" if abs(value) > 2 else "within the typical range, looks normal"
    return f"{value:g} - {band}"


def _timeline_caption(days: float) -> str:
    return f"{days:.0f} day(s) from order to arrival"


def _render_card(entry: TermExplainer) -> str:
    if entry.visual_type == "meter":
        svg = visuals.render_meter_svg(entry.value, entry.slug, color=entry.accent)
        caption = _meter_caption(entry.value, entry.direction)
        slider = (
            f'<input type="range" class="explainer-slider" min="0" max="100" step="1" value="{entry.value:.0f}" '
            f'data-visual="meter" data-slug="{entry.slug}" data-direction="{entry.direction}" '
            f'aria-label="Try a different {entry.term.lower()} value">'
        )
        caption_html = f'<div class="explainer-caption-text" id="{entry.slug}-caption">{caption}</div>'
    elif entry.visual_type == "scale":
        svg = visuals.render_scale_svg(entry.scale_value, entry.slug)
        caption = _scale_caption(entry.scale_value)
        slider = (
            f'<input type="range" class="explainer-slider" min="-3" max="3" step="0.1" value="{entry.scale_value:g}" '
            f'data-visual="scale" data-slug="{entry.slug}" aria-label="Try a different {entry.term.lower()} value">'
        )
        caption_html = f'<div class="explainer-caption-text" id="{entry.slug}-caption">{caption}</div>'
    elif entry.visual_type == "timeline":
        svg = visuals.render_timeline_svg(entry.slug)
        caption = _timeline_caption(entry.days)
        slider = (
            f'<input type="range" class="explainer-slider" min="1" max="60" step="1" value="{entry.days:.0f}" '
            f'data-visual="timeline" data-slug="{entry.slug}" aria-label="Try a different {entry.term.lower()}">'
        )
        caption_html = f'<div class="explainer-caption-text" id="{entry.slug}-caption">{caption}</div>'
    else:  # splitbar
        svg = visuals.render_split_bar_svg(entry.left_label, entry.left_pct, entry.right_label, entry.slug)
        slider = (
            f'<input type="range" class="explainer-slider" min="0" max="100" step="1" value="{entry.left_pct:.0f}" '
            f'data-visual="splitbar" data-slug="{entry.slug}" aria-label="Try a different {entry.term.lower()} split">'
        )
        caption_html = ""  # the 2 segment labels inside the SVG already carry the meaning here

    return (
        f'<div class="explainer-card" style="border-top-color: {entry.accent};">'
        f'<div class="explainer-term">{entry.term}</div>'
        f'<div class="explainer-explanation">{entry.explanation}</div>'
        f'<div class="explainer-visual">{svg}</div>'
        f"{caption_html}"
        f'<label class="explainer-slider-label">Try it{slider}</label>'
        "</div>"
    )


_PAGE_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Glossary - SupplyMind AI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
$google_font_links
<style>
$theme_tokens
  .intro {
    background: var(--surface); border-radius: 12px; padding: 18px 22px; margin-bottom: 24px; font-size: 15px;
    line-height: 1.6; color: var(--ink-soft); box-shadow: var(--shadow);
  }
  .explainer-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; }
  .explainer-card {
    background-color: var(--surface);
    background-image: radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.05) 1px, transparent 0);
    background-size: 16px 16px;
    border-radius: 12px; padding: 18px 20px; box-shadow: var(--shadow);
    border-top: 3px solid var(--border);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  .explainer-card:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19, 24, 38, 0.09); }
  .explainer-term { font-weight: 700; font-size: 17px; color: var(--ink); margin: 0 0 8px; letter-spacing: -0.005em; }
  .explainer-explanation { font-size: 14px; color: var(--ink-soft); line-height: 1.6; margin: 0 0 16px; }
  .explainer-visual { margin-bottom: 8px; }
  .explainer-caption-text { font-size: 13px; font-weight: 650; color: var(--ink); margin-bottom: 14px; }
  .explainer-slider-label {
    display: flex; align-items: center; gap: 10px; font-size: 12px; font-weight: 650; color: var(--ink-faint);
    text-transform: uppercase; letter-spacing: 0.03em; padding-top: 12px; border-top: 1px dashed var(--border-soft);
  }
  .explainer-slider { flex: 1; accent-color: var(--brand); cursor: pointer; }
</style>
</head>
<body>
$onboarding
<div class="page">
  $nav_bar
  $page_header
  <div class="intro">
    Every card below explains one term you'll see elsewhere in the app - the same word a hover tooltip
    defines in one sentence, explained here with a bit more room, a small picture, and a slider to try
    different values yourself.
  </div>
  <div class="explainer-grid">
    $explainer_cards
  </div>
</div>
<script>
function meterBand(value, direction) {
  if (direction === 'positive') {
    if (value >= 70) return 'strong';
    if (value >= 40) return 'moderate';
    return 'low';
  }
  if (value >= 50) return 'high';
  if (value >= 20) return 'moderate';
  return 'low';
}

function scaleCaption(value) {
  var band = Math.abs(value) > 2 ? 'outside the typical range, worth a look' : 'within the typical range, looks normal';
  return value.toFixed(2).replace(/\\.?0+$$/, '') + ' - ' + band;
}

document.querySelectorAll('.explainer-slider').forEach(function(slider) {
  slider.addEventListener('input', function() {
    var slug = slider.dataset.slug;
    var visual = slider.dataset.visual;
    var value = parseFloat(slider.value);
    var caption = document.getElementById(slug + '-caption');

    if (visual === 'meter') {
      var fill = document.getElementById(slug + '-fill');
      fill.setAttribute('width', (2 + (value / 100) * 216).toFixed(1));
      caption.textContent = value.toFixed(0) + ' out of 100 - ' + meterBand(value, slider.dataset.direction);
    } else if (visual === 'scale') {
      var marker = document.getElementById(slug + '-marker');
      var x = 4 + ((value - -3) / 6) * 212;
      marker.setAttribute('cx', x.toFixed(1));
      caption.textContent = scaleCaption(value);
    } else if (visual === 'timeline') {
      caption.textContent = value.toFixed(0) + ' day(s) from order to arrival';
    } else if (visual === 'splitbar') {
      var leftWidth = (value / 100) * 220;
      var rightWidth = 220 - leftWidth;
      document.getElementById(slug + '-left').setAttribute('width', leftWidth.toFixed(1));
      document.getElementById(slug + '-right').setAttribute('x', leftWidth.toFixed(1));
      document.getElementById(slug + '-right').setAttribute('width', rightWidth.toFixed(1));
      document.getElementById(slug + '-left-label').setAttribute('x', Math.max(leftWidth / 2, 4).toFixed(1));
      document.getElementById(slug + '-right-label').setAttribute('x', (leftWidth + Math.max(rightWidth / 2, 4)).toFixed(1));
    }
  });
});
</script>
</body>
</html>""")


def render_page() -> str:
    header_body = (
        "<h1>Glossary</h1>"
        '<div class="meta">Plain-language explanations of the terms used throughout SupplyMind AI, with a '
        "picture and a slider for each one.</div>"
    )
    return _PAGE_TEMPLATE.substitute(
        google_font_links=theme.GOOGLE_FONT_LINKS,
        theme_tokens=theme.TOKENS_CSS,
        nav_bar=theme.render_nav_bar("Glossary"),
        page_header=theme.render_page_header(theme.ICON_GLOSSARY, header_body, accent="accent-2"),
        explainer_cards="\n    ".join(_render_card(entry) for entry in _TERM_EXPLAINERS),
        onboarding=theme.ONBOARDING_HTML,
    )


class LearnHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:
        if self.path != "/":
            http_helpers.send_json(self, 404, {"error": "not found"})
            return
        http_helpers.send_html(self, 200, render_page())


def build_server() -> HTTPServer:
    """Construct (but do not start) this app's HTTPServer - see
    data_console/serve_data_console.py's build_server() for why this is
    split out of main()."""
    port = int(os.environ.get("SUPPLYMIND_LEARN_PORT", DEFAULT_PORT))
    return HTTPServer(("127.0.0.1", port), LearnHandler)


def main() -> int:
    server = build_server()
    print(f"SupplyMind AI Glossary page listening on http://127.0.0.1:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
