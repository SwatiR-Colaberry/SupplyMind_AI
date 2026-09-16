"""Local browser What-If Simulator UI (product spec's "Scenario Simulator" screen).

Same minimal, dependency-free stdlib http.server approach as
chat_interface/serve_chat_ui.py and data_console/serve_data_console.py,
for the same reason: no new package, no new moving part beyond what this
repo already runs. A form posts one baseline inventory position plus a
set of deltas (demand %, lead-time days, safety-stock units, stock
units) to /api/simulate, which runs the exact same
scenario_simulation.simulation.simulate_scenario() the CLI demo and
agents/scenario_simulation_agent.py already use - no simulation logic is
duplicated in JavaScript, same discipline serve_chat_ui.py's own
docstring already states for chat_interface's router/answer logic.

Dev-only: no auth, no TLS, single-threaded, binds to 127.0.0.1 only, and
exits if the port is already taken rather than silently reusing whatever
is already listening there.

Usage:
    python3 -m scenario_simulation.serve_scenario_simulator
    # then open http://127.0.0.1:8767 in a browser
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template
from typing import Any

from local_apps import http_helpers, theme
from inventory_risk.risk_model import InventoryPosition, RiskModelError, StockoutRiskAssessment
from recommendation.stockout_playbook import build_stockout_recommendation, describe_recommendation
from scenario_simulation.simulation import ScenarioImpactAssessment, ScenarioInput, ScenarioValidationError, simulate_scenario

DEFAULT_PORT = 8767

# A simulation request has no legitimate reason to be anywhere near this
# size - same reasoning and same limit as serve_chat_ui.py's own
# MAX_REQUEST_BODY_BYTES.
MAX_REQUEST_BODY_BYTES = 65536

_BASELINE_REQUIRED_FIELDS = ("current_stock", "safety_stock", "daily_demand_rate", "lead_time_days")
_BASELINE_OPTIONAL_FIELDS = ("incoming_stock", "unit_price", "demand_std_dev")
_DELTA_FIELDS = ("demand_change_pct", "lead_time_change_days", "safety_stock_change", "stock_change")

_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SupplyMind AI - What-If Simulator</title>
$google_font_links
<style>
$theme_tokens
  /* Regression (2026-09-12): this used to be 900px while Data Console and
     Live Data both use 1180px - since the nav bar sits at this container's
     left edge, switching to this page (a user reported it as "the page
     center is moving for the last tab") visibly shifted the whole layout
     ~140px as .page's centered margin recalculated for the narrower
     width. Matched to 1180px so every screen's frame stays put. */
  .page { max-width: 1180px; margin: 0 auto; padding: 32px 32px 56px; }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 4px; }
  .meta { color: var(--ink-soft); font-size: 15.5px; margin-bottom: 20px; }
  .layout { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
  .panel {
    /* A user asked for "a few more background pictures" on the app's
       boxes - a very faint dot-grid, same technique the page-header band
       already uses, at half its opacity. */
    background-color: var(--surface);
    background-image: radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.05) 1px, transparent 0);
    background-size: 16px 16px;
    border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; box-shadow: var(--shadow);
  }
  .form-panel { flex: 1; min-width: 320px; }
  .result-panel { flex: 1; min-width: 320px; }
  fieldset { border: 1px solid var(--border); border-radius: 8px; margin: 0 0 14px; padding: 10px 12px; }
  legend { font-size: 13.5px; font-weight: 650; color: var(--ink-soft); padding: 0 4px; }
  label { display: block; font-size: 13.5px; color: var(--ink-soft); margin-bottom: 8px; }
  /* Progressive disclosure for the 3 fields that are genuinely optional
     (they only ever change which extra output shows up, never whether
     the simulation runs at all) - a design-review ask to reduce how
     cluttered the form reads at first glance, since these used to sit at
     the same visual weight as the 4 required fields right above them. */
  .advanced-options { margin-bottom: 8px; }
  .advanced-options summary {
    cursor: pointer; font-size: 12.5px; font-weight: 650; color: var(--brand); list-style: none; margin-bottom: 8px;
  }
  .advanced-options summary::-webkit-details-marker { display: none; }
  .advanced-options summary:hover { color: var(--brand-dark); }
  .advanced-options label:last-child { margin-bottom: 0; }
  input[type=text], input[type=number] {
    width: 100%; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; font-size: 14.5px;
    box-sizing: border-box; margin-top: 3px; background: var(--surface); color: var(--ink);
  }
  input[type=text]:focus, input[type=number]:focus { outline: 2px solid var(--brand-tint); border-color: var(--brand); }
  button {
    padding: 10px 18px; border: 1px solid var(--brand); border-radius: 7px; background: var(--brand);
    color: white; font-size: 15.5px; font-weight: 550; cursor: pointer;
  }
  button:hover { background: var(--brand-dark); border-color: var(--brand-dark); }
  button:disabled { background: var(--ink-faint); border-color: var(--ink-faint); cursor: default; }
  .form-actions { display: flex; gap: 10px; }
  /* A user asked for a way to "remove everything" from the form to start
     entering new data cleanly - an outline style (not solid brand-blue)
     so it doesn't compete with Run Simulation as the primary action. */
  .btn-secondary {
    background: var(--surface); border: 1px solid var(--border); color: var(--ink-soft);
  }
  .btn-secondary:hover { background: var(--canvas); border-color: var(--border); color: var(--ink); }
  .placeholder { color: var(--ink-faint); font-size: 14.5px; }
  .error-box {
    background: var(--danger-tint); border: 1px solid var(--danger-border); color: var(--danger);
    padding: 10px 12px; border-radius: 8px; font-size: 14px;
  }
  .compare-row { display: flex; gap: 12px; margin-bottom: 10px; }
  .compare-col { flex: 1; background: var(--canvas); border-radius: 8px; padding: 10px 12px; }
  /* Regression (2026-09-12): a user asked for the column header ("BASELINE"
     / "PROJECTED") to be bold and for the 2 boxes to have a subtle, distinct
     color each - kept to the existing brand/neutral tint tokens rather than
     new colors, per "keep it very subtle." */
  .compare-baseline { background: var(--neutral-tint); }
  .compare-projected { background: var(--brand-tint); }
  .compare-col h3 { font-size: 13.5px; margin: 0 0 6px; color: var(--ink-soft); font-weight: 750; text-transform: uppercase; letter-spacing: 0.03em; }
  .risk-badge { display: inline-block; padding: 2px 9px; border-radius: 100px; color: white; font-size: 12.5px; font-weight: 650; text-transform: uppercase; }
  .risk-critical { background: var(--danger); }
  .risk-high { background: var(--warning-strong); }
  .risk-medium { background: var(--warning); }
  .risk-low { background: var(--neutral); }
  /* Regression (2026-09-12): this result panel used to render as
     separate <div> lines - a user said it "is not in a list" and asked
     to check every page where information appears after a button click.
     A real <ul>/<li> with visible bullets now backs it. */
  .stat-list { list-style: disc; padding-left: 18px; margin: 8px 0 0; }
  .stat-line { font-size: 14px; color: var(--ink); margin-top: 6px; }
  .worsens { color: var(--danger); font-weight: 650; }
  .improves { color: var(--success); font-weight: 650; }
  .unchanged { color: var(--ink-soft); }
  .recommendation-box {
    margin-top: 14px; background: var(--brand-tint); border-left: 3px solid var(--brand);
    padding: 10px 14px; border-radius: 6px; font-size: 14px; line-height: 1.6;
  }
</style>
</head>
<body>
$onboarding
<div class="page">
  $nav_bar
  $page_header
  <div class="layout">
    <div class="panel form-panel">
      <form id="sim-form">
        <fieldset>
          <legend>Baseline (today)</legend>
          <label>SKU <input type="text" id="sku" value="SKU-1"></label>
          <label>Current stock <input type="number" id="current_stock" value="100" step="any"></label>
          <label><span class="term" data-tooltip="The extra buffer of inventory kept on hand in case of unexpected demand or delays.">Safety stock</span> <input type="number" id="safety_stock" value="20" step="any"></label>
          <label>Daily demand rate <input type="number" id="daily_demand_rate" value="5" step="any"></label>
          <label><span class="term" data-tooltip="How many days it takes for a reordered shipment to arrive.">Lead time (days)</span> <input type="number" id="lead_time_days" value="10" step="any"></label>
          <details class="advanced-options">
            <summary>Advanced options</summary>
            <label>Incoming stock (optional) <input type="number" id="incoming_stock" value="0" step="any"></label>
            <label>Unit price (optional, enables revenue at risk) <input type="number" id="unit_price" step="any"></label>
            <label>Demand std dev (optional, enables stockout probability) <input type="number" id="demand_std_dev" step="any"></label>
          </details>
        </fieldset>
        <fieldset>
          <legend>What if...</legend>
          <label>Demand changes by (%, e.g. 20 for +20%) <input type="number" id="demand_change_pct" value="0" step="any"></label>
          <label>Lead time changes by (days) <input type="number" id="lead_time_change_days" value="0" step="any"></label>
          <label>Safety stock changes by (units) <input type="number" id="safety_stock_change" value="0" step="any"></label>
          <label>Stock on hand changes by (units) <input type="number" id="stock_change" value="0" step="any"></label>
        </fieldset>
        <div class="form-actions">
          <button type="submit" id="submit-btn">Run Simulation</button>
          <button type="reset" id="reset-btn" class="btn-secondary">Reset</button>
        </div>
      </form>
    </div>
    <div class="panel result-panel" id="result-panel">
      $initial_placeholder
    </div>
  </div>
</div>
<script>
const form = document.getElementById('sim-form');
const resultPanel = document.getElementById('result-panel');
const submitBtn = document.getElementById('submit-btn');
// Captured once, before any simulation runs, so the "reset" handler
// below can restore this exact server-rendered markup instead of a
// hand-copied duplicate that could drift from it.
const initialResultHtml = resultPanel.innerHTML;

function num(id) {
  const el = document.getElementById(id);
  return el.value === '' ? null : parseFloat(el.value);
}

function riskBadge(level) {
  return '<span class="risk-badge risk-' + level + '">' + level + '</span>';
}

function fmtDays(days) {
  return (days === null || days === Infinity) ? 'no measurable demand' : days.toFixed(1) + ' day(s)';
}

// A design-review ask to show charts instead of raw numbers wherever the
// data supports it - a 0-100 fill meter, same visual language as
// learn/visuals.py's render_meter_svg() (a rounded track, a colored
// fill, a caption below), reused here in JS since this page's result
// panel is entirely client-rendered and has no access to that Python
// module. Colored by risk_level rather than a fixed scale so it agrees
// with the badge already shown right above it.
const _RISK_METER_COLORS = {critical: 'var(--danger)', high: 'var(--warning-strong)', medium: 'var(--warning)', low: 'var(--neutral)'};

function riskMeterSvg(score, level) {
  const clamped = Math.max(0, Math.min(100, score));
  const fillWidth = (2 + (clamped / 100) * 216).toFixed(1);
  const color = _RISK_METER_COLORS[level] || 'var(--brand)';
  return '<svg viewBox="0 0 220 40" class="explainer-svg" role="img" aria-label="Risk score ' + clamped.toFixed(1) + ' out of 100">' +
    '<rect x="0" y="4" width="220" height="16" rx="8" fill="var(--canvas)" stroke="var(--border)"></rect>' +
    '<rect x="0" y="4" width="' + fillWidth + '" height="16" rx="8" fill="' + color + '"></rect>' +
    '<text x="110" y="34" text-anchor="middle" class="explainer-caption">' + clamped.toFixed(1) + ' out of 100</text>' +
    '</svg>';
}

function renderAssessment(label, a, colClass) {
  // Fixed tooltip text, not user data - see chat_interface/serve_chat_ui.py's
  // matching glossary tooltips for the same reasoning; added after a user
  // said "risk score" (and later, other jargon in general) "does not
  // explain anything" alone. Wording matches dashboard/render.py's own
  // GLOSSARY dict, kept in sync by hand since this is JS, not Python.
  let html = '<div class="compare-col ' + colClass + '"><h3>' + label + '</h3>';
  html += riskBadge(a.risk_level);
  html += riskMeterSvg(a.risk_score, a.risk_level);
  html += '<ul class="stat-list">';
  html += '<li class="stat-line">' + fmtDays(a.days_of_supply) + ' of supply</li>';
  html += '<li class="stat-line"><span class="term" data-tooltip="The estimated chance this item runs out of stock before the next shipment arrives.">Stockout probability</span>: ' + Math.round(a.stockout_probability * 100) + '%</li>';
  html += '<li class="stat-line"><span class="term" data-tooltip="A single 0-100 score combining how likely a stockout is and how severe it would be. Higher means more urgent.">Risk score</span>: ' + a.risk_score.toFixed(1) + '/100</li>';
  if (a.revenue_at_risk) {
    html += '<li class="stat-line"><span class="term" data-tooltip="The estimated sales value that could be lost if this risk goes unaddressed.">Revenue at risk</span>: $$' + a.revenue_at_risk.toFixed(2) + '</li>';
  }
  html += '</ul>';
  html += '</div>';
  return html;
}

// The native `reset` event only restores each <input>'s own value
// attribute - it doesn't know about resultPanel's own innerHTML, which a
// prior run may have replaced with a comparison or an error box. A user
// asked for a reset that "removes everything," so this clears the result
// panel back to its own starting placeholder in the same handler.
form.addEventListener('reset', () => {
  resultPanel.innerHTML = initialResultHtml;
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  submitBtn.disabled = true;
  resultPanel.innerHTML = '<div class="placeholder">Running simulation...</div>';
  try {
    const payload = {
      scenario_name: 'what-if',
      baseline: {
        sku: document.getElementById('sku').value || 'SKU-1',
        current_stock: num('current_stock'),
        safety_stock: num('safety_stock'),
        daily_demand_rate: num('daily_demand_rate'),
        lead_time_days: num('lead_time_days'),
        incoming_stock: num('incoming_stock'),
        unit_price: num('unit_price'),
        demand_std_dev: num('demand_std_dev'),
      },
      deltas: {
        demand_change_pct: (num('demand_change_pct') || 0) / 100.0,
        lead_time_change_days: num('lead_time_change_days') || 0,
        safety_stock_change: num('safety_stock_change') || 0,
        stock_change: num('stock_change') || 0,
      },
    };
    const response = await fetch('$api_path', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      resultPanel.innerHTML = '<div class="error-box">' + (data.error || 'Simulation failed.') + '</div>';
      return;
    }
    let directionClass = 'unchanged';
    let directionText = 'leaves risk unchanged';
    if (data.risk_level_changed) {
      directionClass = data.direction === 'worsens' ? 'worsens' : 'improves';
      directionText = data.direction + ' risk';
    }
    let html = '<div class="' + directionClass + '" style="margin-bottom:10px;">This scenario ' + directionText + '.</div>';
    html += '<div class="compare-row">' + renderAssessment('Baseline', data.baseline, 'compare-baseline') + renderAssessment('Projected', data.projected, 'compare-projected') + '</div>';
    html += '<div class="recommendation-box">' + data.recommendation_line + '</div>';
    resultPanel.innerHTML = html;
  } catch (err) {
    resultPanel.innerHTML = '<div class="error-box">Could not reach the server.</div>';
  } finally {
    submitBtn.disabled = false;
  }
});
</script>
</body>
</html>""")


def _assessment_payload(assessment: StockoutRiskAssessment) -> dict[str, Any]:
    days = None if assessment.days_of_supply == float("inf") else assessment.days_of_supply
    return {
        "risk_level": assessment.risk_level,
        "days_of_supply": days,
        "stockout_probability": assessment.stockout_probability,
        "risk_score": assessment.risk_score,
        "revenue_at_risk": assessment.revenue_at_risk,
        "confidence": assessment.confidence,
        "detail": assessment.detail,
    }


def _parse_baseline(raw: dict[str, Any]) -> InventoryPosition:
    kwargs: dict[str, Any] = {"sku": str(raw.get("sku") or "SKU-1")}
    for field_name in _BASELINE_REQUIRED_FIELDS:
        value = raw.get(field_name)
        if value is None:
            raise ScenarioValidationError(f"{field_name} is required")
        kwargs[field_name] = float(value)
    for field_name in _BASELINE_OPTIONAL_FIELDS:
        value = raw.get(field_name)
        if value is not None:
            kwargs[field_name] = float(value)
    return InventoryPosition(**kwargs)


def _parse_scenario(payload: dict[str, Any]) -> ScenarioInput:
    baseline_raw = payload.get("baseline")
    if not isinstance(baseline_raw, dict):
        raise ScenarioValidationError("baseline is required")
    baseline = _parse_baseline(baseline_raw)

    deltas_raw = payload.get("deltas") or {}
    delta_kwargs = {f: float(deltas_raw[f]) for f in _DELTA_FIELDS if deltas_raw.get(f) is not None}

    return ScenarioInput(scenario_name=str(payload.get("scenario_name") or "what-if"), baseline=baseline, **delta_kwargs)


def _impact_payload(impact: ScenarioImpactAssessment) -> dict[str, Any]:
    if impact.risk_level_changed:
        rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        direction = "worsens" if rank[impact.projected.risk_level] > rank[impact.baseline.risk_level] else "improves"
    else:
        direction = "unchanged"
    recommendation = build_stockout_recommendation(impact.projected)
    return {
        "baseline": _assessment_payload(impact.baseline),
        "projected": _assessment_payload(impact.projected),
        "risk_level_changed": impact.risk_level_changed,
        "direction": direction,
        "detail": impact.detail,
        "recommendation_line": describe_recommendation(recommendation),
    }


def render_page(api_path: str = "/api/simulate") -> str:
    """`api_path` (default "/api/simulate", correct for this module's own
    standalone server) is overridden by local_apps/unified_server.py to
    "/simulate/api/simulate" - the path the form's JS actually needs to
    POST to once this page is mounted under a "/simulate" prefix rather
    than served from an origin's root. Regression (2026-09-11): before
    this parameter existed, the merged server's own routing was correctly
    prefixed, but this page's embedded JS still hardcoded the un-prefixed
    "/api/simulate" - a request the merged server has no route for -
    which the browser (not curl, and not a test hitting the API path
    directly, which is how this shipped without being caught) surfaced as
    a plain "not found" in the results panel, from the merged server's
    own generic 404 JSON body.
    """
    header_body = (
        "<h1>What-If Simulator</h1>"
        '<div class="meta">Change one or more assumptions below and see the projected impact on stockout risk, '
        "before committing to anything for real.</div>"
    )
    return _PAGE_TEMPLATE.substitute(
        google_font_links=theme.GOOGLE_FONT_LINKS,
        theme_tokens=theme.TOKENS_CSS,
        nav_bar=theme.render_nav_bar("What-If Simulator"),
        page_header=theme.render_page_header(theme.ICON_SIMULATOR, header_body, accent="success"),
        initial_placeholder=theme.render_empty_state(
            theme.ICON_SIMULATOR, "Nothing simulated yet", "Run a simulation to see the projected impact here."
        ),
        onboarding=theme.ONBOARDING_HTML,
        api_path=api_path,
    )


def run_simulation(raw_body: bytes) -> tuple[int, dict]:
    """Parse one /api/simulate POST body and run it through simulate_scenario().

    Returns (http_status, json_payload) rather than writing to a socket
    directly, so both ScenarioSimulatorHandler's own standalone server and
    local_apps/unified_server.py's merged server can share this one
    implementation instead of each parsing/simulating a request its own
    way.
    """
    try:
        payload = json.loads(raw_body or b"{}")
        if not isinstance(payload, dict):
            raise TypeError("request body must be a JSON object")
        scenario = _parse_scenario(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return 400, {"error": f"invalid request: {exc}"}

    try:
        impact = simulate_scenario(scenario)
    except (ScenarioValidationError, RiskModelError) as exc:
        return 400, {"error": str(exc)}

    return 200, _impact_payload(impact)


class ScenarioSimulatorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:
        if self.path != "/":
            http_helpers.send_json(self, 404, {"error": "not found"})
            return
        http_helpers.send_html(self, 200, render_page())

    def do_POST(self) -> None:
        if self.path != "/api/simulate":
            http_helpers.send_json(self, 404, {"error": "not found"})
            return

        try:
            raw_body = http_helpers.read_request_body(self, MAX_REQUEST_BODY_BYTES)
        except ValueError as exc:
            http_helpers.send_json(self, 400, {"error": f"invalid request: {exc}"})
            return

        status, response_payload = run_simulation(raw_body)
        http_helpers.send_json(self, status, response_payload)


def build_server() -> HTTPServer:
    """Construct (but do not start) this app's HTTPServer - see
    data_console/serve_data_console.py's build_server() for why this is
    split out of main()."""
    port = int(os.environ.get("SUPPLYMIND_SCENARIO_SIMULATOR_PORT", DEFAULT_PORT))
    return HTTPServer(("127.0.0.1", port), ScenarioSimulatorHandler)


def main() -> int:
    server = build_server()
    print(f"SupplyMind AI What-If Simulator listening on http://127.0.0.1:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
