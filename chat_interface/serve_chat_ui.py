"""Local browser chat UI for STORY-010 (REQ-016 / AI Chat Interface).

A minimal, dependency-free HTTP server (Python's stdlib http.server - no
new dependency, per CLAUDE.md's 12-factor "explicit dependencies"
principle) that serves one self-contained HTML page with a text box, and
answers every submitted query through the exact same ChatEvaluator /
ChatAuditStore path chat_interface/run_sample_chat_interface.py already
exercises. No query-classification or answer-formatting logic is
duplicated in JavaScript.

A purely static, client-side-only chat widget was considered and
rejected: rendering a DashboardSnapshot once into HTML (the way
dashboard/render.py already does for a read-only dashboard) works fine
there, but a chat box has to run real classification/lookup code on every
submission and durably log every interaction (the Trust AC). Baking
router.classify_query()/chat.answer_query() into JavaScript would mean a
second copy of that logic to keep in sync by hand, and a browser page has
no way to append to this repo's JSONL audit trail on its own - any
interaction typed into a static page would silently bypass the Trust AC
for that entry point. Routing every submission through this tiny local
server instead keeps ChatEvaluator as the single source of truth for
both the answer and the audit record, so a browser-typed query is exactly
as audited as a query run through the batch demo.

Dev-only, on the same footing as scripts/local_test_db.py's own local
Postgres server - not part of chat_interface/'s own trust-spine
(router.py/chat.py/audit_trail.py/evaluator.py all work with zero server
involved), just a way to interact with it from a browser instead of a
script. Not a production service: no auth, no TLS, single-threaded,
binds to 127.0.0.1 only, and exits if the port is already taken rather
than silently reusing whatever is already listening there.

Usage:
    python3 -m chat_interface.serve_chat_ui
    # then open http://127.0.0.1:8765 in a browser

    # against real Postgres data instead of the synthetic "healthy" set:
    eval "$(python3 scripts/local_test_db.py)"
    python3 -m chat_interface.serve_chat_ui
"""

from __future__ import annotations

import html as html_lib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template

from local_apps import http_helpers, theme
from chat_interface.evaluator import ChatEvaluator
from chat_interface.run_sample_chat_interface import (
    build_healthy_snapshot,
    build_real_data_snapshot,
    chat_audit_store,
)
import dashboard.live_refresh as live_refresh
import dashboard.render as dashboard_render
from dashboard.charts import ChartSpec, render_bar_chart
from dashboard.metrics import DashboardSnapshot
from data_integration.config import MissingConfigError, load_postgres_config

DEFAULT_PORT = 8765

# A chat query has no legitimate reason to be anywhere near this size;
# capping it here means a bad-faith or malformed Content-Length can never
# make this single-threaded dev server block reading an enormous (or,
# per _read_request_body's own validation, negative) body. 64 KiB is
# generously larger than any real question typed into the text box.
MAX_REQUEST_BODY_BYTES = 65536

# string.Template's $-placeholders, not str.format()'s {}, since this
# page embeds a nontrivial amount of literal CSS and JavaScript - with
# .format() every literal brace in that CSS/JS has to be escaped as {{/}}
# by hand, and a future edit that adds one un-escaped brace either raises
# KeyError or silently mangles the page. $-placeholders need no such
# escaping for CSS/JS, which use {} constantly but never $.
_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SupplyMind AI Chat</title>
$google_font_links
<style>
$theme_tokens
  /* Regression (2026-09-12): this used to be 900px while Data Console and
     Live Data both use 1180px - since the nav bar sits at this container's
     left edge, switching to this page (or What-If Simulator, same bug)
     visibly shifted the whole layout ~140px as .page's centered margin
     recalculated for the narrower width. Matched to 1180px so every
     screen's frame stays put; .chat-box below still caps the actual
     transcript at a comfortable reading width inside it. */
  .page { max-width: 1180px; margin: 0 auto; padding: 32px 32px 56px; }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 4px; }
  .meta { color: var(--ink-soft); font-size: 15.5px; margin-bottom: 20px; }
  .chat-box { max-width: 720px; }
  .transcript { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; min-height: 60px; }
  .bubble {
    /* A user asked for "a few more background pictures" on the app's
       boxes - a very faint dot-grid, same technique the page-header band
       already uses, at half its opacity. */
    background-color: var(--surface);
    background-image: radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.05) 1px, transparent 0);
    background-size: 16px 16px;
    border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 16px; box-shadow: var(--shadow); font-size: 14.5px;
    /* A user asked for the page's items to look "more dynamic" - a subtle
       hover lift, same technique dashboard/render.py's own .tile uses. */
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  .bubble:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19, 24, 38, 0.09); }
  .bubble-query { font-weight: 600; color: var(--ink); }
  .bubble-answer { margin-top: 6px; color: var(--ink); }
  .bubble-answered { border-left: 3px solid var(--success); }
  .bubble-data_unavailable { border-left: 3px solid var(--warning); }
  .bubble-unsupported { border-left: 3px solid var(--neutral); }
  .bubble-error { border-left: 3px solid var(--danger); }
  .meta-line { font-size: 12.5px; color: var(--ink-faint); margin-top: 4px; }
  form { display: flex; gap: 8px; }
  input[type=text] {
    flex: 1; padding: 10px 12px; border: 1px solid var(--border); border-radius: 7px; font-size: 15.5px;
    background: var(--surface); color: var(--ink);
  }
  input[type=text]:focus { outline: 2px solid var(--brand-tint); border-color: var(--brand); }
  button {
    padding: 10px 18px; border: 1px solid var(--brand); border-radius: 7px; background: var(--brand);
    color: white; font-size: 15.5px; font-weight: 550; cursor: pointer;
  }
  button:hover { background: var(--brand-dark); border-color: var(--brand-dark); }
  button:disabled { background: var(--ink-faint); border-color: var(--ink-faint); cursor: default; }
  .placeholder { color: var(--ink-faint); font-size: 14.5px; }
  .chart-card { background: var(--canvas); border-radius: 7px; padding: 10px 12px; margin-top: 8px; }
  .chart-title { font-size: 13px; font-weight: 650; color: var(--ink); margin: 0 0 6px; }
  .chart-svg { width: 100%; height: auto; display: block; }
  .chart-label { font-size: 11.5px; fill: var(--ink-soft); }
  .chart-value { font-size: 11.5px; fill: var(--ink-faint); }
  .chart-footnote { font-size: 12px; color: var(--ink-faint); margin-top: 6px; }
  /* Added 2026-09-12 after a user asked for "3 or 4 more examples" on
     this screen - a first-time reader had only the input's own
     placeholder text to go on for what this can actually answer. */
  .example-chips-label { font-size: 12.5px; color: var(--ink-faint); margin-bottom: 8px; }
  .example-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .example-chip {
    padding: 7px 14px; border: 1px solid var(--border); border-radius: 100px; background: var(--surface);
    color: var(--brand-dark); font-size: 13px; font-weight: 550; cursor: pointer; font-family: inherit;
  }
  .example-chip:hover { background: var(--brand-tint); border-color: var(--brand); }
</style>
</head>
<body>
$onboarding
<div class="page">
  $nav_bar
  $page_header
  <div class="chat-box">
    <div class="transcript" id="transcript">
      $initial_placeholder
    </div>
    <div class="example-chips-label">Try asking:</div>
    <div class="example-chips" id="example-chips">
      <button type="button" class="example-chip">What's our stockout risk?</button>
      <button type="button" class="example-chip">Show me the top 10 suppliers</button>
      <button type="button" class="example-chip">Any demand anomalies this month?</button>
      <button type="button" class="example-chip">Which shipments are delayed?</button>
    </div>
    <form id="chat-form">
      <input type="text" id="query" placeholder="Ask about stockout risk, suppliers, shipments, demand forecast, or a specific SKU/PO..." autocomplete="off" />
      <button type="submit" id="submit-btn">Ask</button>
    </form>
  </div>
</div>
<script>
const form = document.getElementById('chat-form');
const input = document.getElementById('query');
const transcript = document.getElementById('transcript');
const submitBtn = document.getElementById('submit-btn');
let firstMessage = true;

// requestSubmit() (not form.submit()) fires the form's own 'submit'
// listener below, exactly as if the user had typed the chip's text and
// pressed "Ask" themselves - form.submit() would bypass that listener
// entirely and do nothing, since this form has no server-side action.
document.querySelectorAll('.example-chip').forEach(function(chip) {
  chip.addEventListener('click', function() {
    input.value = chip.textContent;
    form.requestSubmit();
  });
});

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query) return;
  if (firstMessage) {
    transcript.innerHTML = '';
    firstMessage = false;
  }
  submitBtn.disabled = true;
  input.disabled = true;
  try {
    const response = await fetch('$api_path', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query}),
    });
    const data = await response.json();
    const statusClass = 'bubble-' + (data.status || 'error');
    const bubble = document.createElement('div');
    bubble.className = 'bubble ' + statusClass;
    // Chart SVGs come from the server already built as trusted markup
    // (dashboard/charts.py's render_bar_chart escapes every label before
    // this page ever sees it - same discipline dashboard/render.py uses
    // for its own agent-derived text) - inserted as-is via innerHTML,
    // never through escapeHtml() like the plain-text fields above, or
    // the chart would render as literal <svg> text instead of a chart.
    const chartsHtml = (data.charts || []).map(function (chart) {
      const footnote = chart.omitted_count
        ? '<div class="chart-footnote">+' + chart.omitted_count + ' more not shown</div>' : '';
      return '<div class="chart-card"><div class="chart-title">' + escapeHtml(chart.title) + '</div>' +
        chart.svg + footnote + '</div>';
    }).join('');
    // answer_html is server-built trusted markup (dashboard/render.py's
    // render_answer_lines() escapes the underlying text itself before
    // ever adding a highlight/glossary span) - same trust boundary as
    // chartsHtml above, not user data, so it's inserted as-is rather than
    // through escapeHtml(). Falls back to the old plain-text rendering if
    // an older response somehow lacks it. Regression this fixes: the
    // answer used to render as one dense run-on paragraph with no list
    // structure and no explanation for jargon like "z-score."
    const answerHtml = data.answer_html || ('<div>' + escapeHtml(data.answer) + '</div>');
    bubble.innerHTML =
      '<div class="bubble-query">' + escapeHtml(query) + '</div>' +
      '<div class="bubble-answer">' + answerHtml + '</div>' +
      '<div class="meta-line">status: ' + escapeHtml(data.status) +
      (data.topic ? ' | topic: ' + escapeHtml(data.topic) : '') +
      (data.subject ? ' | about: ' + escapeHtml(data.subject) : '') +
      // Fixed tooltip text on a code-authored span, not user data - safe to
      // insert into innerHTML directly (see this file's own escapeHtml()
      // discipline for the actually-untrusted fields around it). Added
      // after a user said "confidence" "does not explain anything" alone.
      (data.confidence !== null && data.confidence !== undefined ? ' | <span class="term" data-tooltip="How sure the system is in this answer, based on how much data was available and how much the underlying checks agreed with each other. Higher is more certain.">confidence</span>: ' + Math.round(data.confidence * 100) + '%' : '') +
      ' | interaction: ' + escapeHtml(data.interaction_id) + '</div>' +
      chartsHtml;
    transcript.appendChild(bubble);
  } catch (err) {
    const bubble = document.createElement('div');
    bubble.className = 'bubble bubble-error';
    bubble.textContent = 'Chat interface failure: could not reach the server.';
    transcript.appendChild(bubble);
  } finally {
    submitBtn.disabled = false;
    input.disabled = false;
    input.value = '';
    input.focus();
    transcript.scrollTop = transcript.scrollHeight;
  }
});
</script>
</body>
</html>""")


def _chart_payload(spec: ChartSpec) -> dict:
    """One ChartSpec as the JSON shape the client's chartsHtml renderer expects.

    `svg` is already-escaped, trusted markup (render_bar_chart HTML-escapes
    every bar label before returning it - same discipline dashboard/
    render.py's own _esc() applies to agent-derived text) - the client
    inserts it via innerHTML rather than escapeHtml(), same as every other
    piece of server-rendered HTML this repo already ships that way.
    """
    return {"title": spec.title, "question": spec.question, "svg": render_bar_chart(spec), "omitted_count": spec.omitted_count}


def render_page(snapshot: DashboardSnapshot, data_source: str, api_path: str = "/api/chat") -> str:
    """`api_path` (default "/api/chat", correct for this module's own
    standalone server) is overridden by local_apps/unified_server.py to
    "/chat/api/chat" - the path this page's own JS actually needs to POST
    to once mounted under a "/chat" prefix instead of served from an
    origin's root. Same regression class as scenario_simulator.py's
    render_page() - see that function's docstring for the full story
    (a real "not found" a user hit in their browser, invisible to a test
    or curl call hitting the API path directly rather than exercising the
    page's own embedded fetch call).
    """
    header_body = (
        "<h1>SupplyMind AI Chat</h1>"
        f'<div class="meta">answering from dashboard {html_lib.escape(snapshot.dashboard_id)} '
        f"({html_lib.escape(data_source)}) - generated {html_lib.escape(snapshot.generated_at)}</div>"
    )
    return _PAGE_TEMPLATE.substitute(
        google_font_links=theme.GOOGLE_FONT_LINKS,
        theme_tokens=theme.TOKENS_CSS,
        nav_bar=theme.render_nav_bar("AI Assistant"),
        page_header=theme.render_page_header(theme.ICON_CHAT, header_body, accent="accent-2"),
        initial_placeholder=theme.render_empty_state(
            theme.ICON_CHAT, "No questions yet", 'Ask a question, e.g. "what\'s our stockout risk?"'
        ),
        onboarding=theme.ONBOARDING_HTML,
        api_path=api_path,
    )


def answer_chat_query(raw_body: bytes, evaluator: ChatEvaluator, snapshot: DashboardSnapshot) -> tuple[int, dict]:
    """Parse one /api/chat POST body and answer it against `snapshot` via `evaluator`.

    Returns (http_status, json_payload) rather than writing to a socket
    directly, so both ChatUIHandler's own standalone server and
    local_apps/unified_server.py's merged server can share this one
    implementation instead of each parsing/answering a chat request its
    own way.
    """
    try:
        payload = json.loads(raw_body or b"{}")
        query_text = payload["query"]
        if not isinstance(query_text, str):
            raise TypeError("'query' must be a string")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        # Same failure path _read_request_body's own ValueError takes at
        # the caller - a body that read fine but doesn't contain a usable
        # query, distinct from a query answer_query() itself would
        # handle, rejected here before it ever reaches ChatEvaluator.
        return 400, {"error": f"invalid request: {exc}"}

    run = evaluator.run(query_text, snapshot)
    if run.outcome == "crashed":
        error_answer = f"Chat interface failure: {run.crash_error}"
        return 200, {
            "interaction_id": run.interaction_id,
            "status": "error",
            "answer": error_answer,
            "answer_html": dashboard_render.render_answer_lines(error_answer),
            "topic": None,
            "subject": None,
            "confidence": None,
            "charts": [],
        }

    response = run.response
    return 200, {
        "interaction_id": run.interaction_id,
        "status": response.status,
        "answer": response.answer,
        # A user pointed out this answer rendered as one dense run-on
        # paragraph with unexplained jargon ("z-score", "stockout
        # probability", ...) - answer_html reuses dashboard/render.py's
        # own condense/split/highlight/glossary pipeline (already proven
        # out for the Live Data screen's tile headlines) so both screens
        # explain the same terms the same way from one implementation.
        # `answer` (plain text) is kept unchanged for any other consumer
        # of this API - this is an additive field, not a contract change.
        "answer_html": dashboard_render.render_answer_lines(response.answer),
        "topic": response.topic,
        "subject": response.subject,
        "confidence": response.confidence,
        "charts": [_chart_payload(spec) for spec in response.chart_specs],
    }


def choose_snapshot() -> tuple[DashboardSnapshot, str]:
    """Pick real Postgres data when fully configured, else the synthetic "healthy" set.

    Gated on load_postgres_config() actually succeeding - the same
    canonical check data_integration/postgres_connector.py itself uses -
    rather than a single hardcoded env var name. An earlier revision only
    checked SUPPLYMIND_PG_HOST; with that var set but one of the other
    three required ones (SUPPLYMIND_PG_DATABASE/_USER/_PASSWORD) missing
    or misspelled, every dataset pull would fail inside
    build_real_data_snapshot() while the UI's own banner still claimed
    "real Postgres data" - a misleading label for what was actually an
    all-error snapshot. Falling back to synthetic data here on any
    MissingConfigError keeps the label honest without duplicating the
    four required variable names.
    """
    try:
        load_postgres_config()
    except MissingConfigError:
        return build_healthy_snapshot(), "synthetic demo data - set SUPPLYMIND_PG_HOST etc. for real data"
    return build_real_data_snapshot(), "real Postgres data"


def current_snapshot(startup_snapshot: DashboardSnapshot, startup_data_source: str) -> tuple[DashboardSnapshot, str]:
    """Prefer whatever Data Console's "Run Analysis" last produced, if it's ever been clicked; else `startup_snapshot`/`startup_data_source` (choose_snapshot()'s own result from server start).

    Before this existed, the chat UI's snapshot was chosen once at server
    startup and never updated - a real user reported this as "does the AI
    only work with Postgres, or do I need to reconnect my data here too"
    (2026-09-11), since neither reconnecting a source nor clicking "Run
    Analysis" changed what chat answered from.
    dashboard.live_refresh.refresh_real_data_dashboard() (Run Analysis's
    own server-side handler) already builds its snapshot from whatever
    data_console currently has mapped - a live table, an uploaded CSV, or
    a Google Sheet, not just Postgres - so reusing its latest result here
    means chat now answers from the exact same data Live Data shows,
    refreshed the same way, instead of running a second, independently
    stale pipeline. Called per-request (not cached at this layer) so a
    "Run Analysis" click takes effect on the very next chat question with
    no server restart needed.
    """
    refreshed = live_refresh.latest_real_data_snapshot()
    if refreshed is not None:
        return refreshed, "real data (last refreshed via Run Analysis)"
    return startup_snapshot, startup_data_source


class ChatUIServer(HTTPServer):
    """Holds this server's snapshot/evaluator as instance state, not handler class state.

    BaseHTTPRequestHandler is instantiated fresh per request, so per-server
    configuration has to live somewhere else. An earlier revision set it
    directly on the ChatUIHandler *class* (ChatUIHandler.evaluator = ...),
    which works only because this process ever runs one server - two
    ChatUIServer instances in the same process (or a future switch to
    ThreadingHTTPServer) would silently clobber each other's snapshot
    through that shared class state. Storing it on the server instance
    instead (self.server.evaluator, read from the handler) is the standard
    idiom for passing per-server state into BaseHTTPRequestHandler.
    """

    def __init__(self, address: tuple[str, int], evaluator: ChatEvaluator, snapshot: DashboardSnapshot, data_source: str) -> None:
        super().__init__(address, ChatUIHandler)
        self.evaluator = evaluator
        self.snapshot = snapshot
        self.data_source = data_source


class ChatUIHandler(BaseHTTPRequestHandler):
    server: ChatUIServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # chat_interface's own JSON logger already covers every chat interaction

    def do_GET(self) -> None:
        if self.path != "/":
            http_helpers.send_json(self, 404, {"error": "not found"})
            return
        snapshot, data_source = current_snapshot(self.server.snapshot, self.server.data_source)
        html = render_page(snapshot, data_source)
        http_helpers.send_html(self, 200, html)

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            http_helpers.send_json(self, 404, {"error": "not found"})
            return

        try:
            raw_body = http_helpers.read_request_body(self, MAX_REQUEST_BODY_BYTES)
        except ValueError as exc:
            # Malformed/absurd Content-Length ("User interface display
            # issues" / malformed-request failure path): reject cleanly
            # with 400 rather than letting an uncaught ValueError propagate
            # (previously: a non-numeric header crashed the handler with no
            # response at all) or blocking forever on self.rfile.read() with
            # a negative length (previously: wedged this single-threaded
            # server for every other client until the connection closed).
            http_helpers.send_json(self, 400, {"error": f"invalid request: {exc}"})
            return

        snapshot, _ = current_snapshot(self.server.snapshot, self.server.data_source)
        status, response_payload = answer_chat_query(raw_body, self.server.evaluator, snapshot)
        http_helpers.send_json(self, status, response_payload)


def build_server() -> tuple[ChatUIServer, str]:
    """Construct (but do not start) this app's ChatUIServer, and the
    data_source label it's serving from - see data_console/
    serve_data_console.py's build_server() for why this is split out of
    main()."""
    port = int(os.environ.get("SUPPLYMIND_CHAT_UI_PORT", DEFAULT_PORT))
    snapshot, data_source = choose_snapshot()
    evaluator = ChatEvaluator(chat_audit_store())
    server = ChatUIServer(("127.0.0.1", port), evaluator, snapshot, data_source)
    return server, data_source


def main() -> int:
    server, data_source = build_server()
    print(f"SupplyMind AI Chat listening on http://127.0.0.1:{server.server_port} ({data_source})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
