"""Local browser UI for connecting to and browsing a database (data-console, Slice 1).

Slice 1 scope only: connect to Postgres, list its tables, and for a
selected table show its real columns plus how they compare against each of
this repo's three known required datasets (data_console/column_requirements.py).
No query building, no joins, no CSV upload, no chat yet - those are later
slices, same "one step at a time" build order used for
chat_interface/serve_chat_ui.py.

Same dependency-free stdlib http.server approach as
chat_interface/serve_chat_ui.py, for the same reason: no new package, no
new moving part beyond what this repo already runs. Dev-only - no auth,
no TLS, single-threaded, binds to 127.0.0.1 only.

Usage:
    python3 -m data_console.serve_data_console
    # then open http://127.0.0.1:8766 in a browser

    # against the repo's own seeded local Postgres:
    eval "$(python3 scripts/local_test_db.py)"
    python3 -m data_console.serve_data_console
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template
from urllib.parse import unquote

from data_console import schema_inspector
from data_console.logging_setup import get_logger
from data_console.requirements_check import RequirementCheckResult, check_against_all_known_datasets
from data_integration.config import MissingConfigError
from data_integration.postgres_connector import PostgresIntegrationError

logger = get_logger()

DEFAULT_PORT = 8766

_COLUMNS_PATH_RE = re.compile(r"^/api/tables/([^/]+)/columns$")

_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Connect Your Data</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #f5f6f8; color: #1a1a1a; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 14px; margin: 0 0 8px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  .intro { background: white; border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; font-size: 13px; color: #333; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
  .layout { display: flex; gap: 16px; align-items: flex-start; }
  .panel { background: white; border-radius: 8px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
  .tables-panel { width: 220px; flex-shrink: 0; }
  .detail-panel { flex: 1; min-width: 0; }
  .table-item { display: block; width: 100%; text-align: left; padding: 8px 10px; border: none; background: none; border-radius: 6px; cursor: pointer; font-size: 13px; color: #1a1a1a; }
  .table-item:hover { background: #f0f2f5; }
  .table-item.selected { background: #e3edfb; font-weight: 600; }
  .placeholder { color: #888; font-size: 13px; }
  .not-connected { background: #fff3cd; border: 1px solid #ffe69c; padding: 12px 14px; border-radius: 6px; font-size: 13px; }
  .not-connected code { background: #00000010; padding: 1px 4px; border-radius: 3px; }
  table.col-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 16px; }
  table.col-table th, table.col-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }
  .req-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .req-card { border: 1px solid #eee; border-radius: 6px; padding: 10px 12px; }
  .req-card.satisfied { border-left: 4px solid #2e7d32; }
  .req-card.unsatisfied { border-left: 4px solid #c62828; }
  .req-line { font-size: 12px; margin: 2px 0; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-yes { background: #2e7d32; }
  .dot-no { background: #c62828; }
</style>
</head>
<body>
  <h1>Connect Your Data</h1>
  <div class="meta">data console - browse what's in your database before selecting anything to analyze</div>
  <div class="intro">
    This page reads what's already in your database and shows it to you plainly - it doesn't change or
    move anything. Click a table on the left to see its columns, and whether it already has everything
    one of our checks (Customer Orders, Inventory, Delivery Records) needs.
  </div>
  <div id="content">
    <div class="placeholder">Loading...</div>
  </div>
<script>
// Every value that came out of the connected database (table names, column
// names, data types) is built into the page as real DOM nodes via
// textContent/dataset - never by concatenating it into an HTML string -
// so a table or column named with an unusual character can't be
// misread as markup. Only the fixed, code-authored strings from
// data_console/column_requirements.py (labels, descriptions, examples)
// are ever treated as safe to combine with markup, and even those go
// through textContent below rather than innerHTML.
function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function el(tag, opts) {
  const node = document.createElement(tag);
  opts = opts || {};
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = opts.text;
  return node;
}

async function loadTables() {
  const content = document.getElementById('content');
  const resp = await fetch('/api/tables');
  const data = await resp.json();
  clear(content);
  if (!data.connected) {
    content.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }
  if (data.tables.length === 0) {
    content.appendChild(el('div', {className: 'not-connected', text: 'Connected, but no tables were found in this database yet.'}));
    return;
  }

  const layout = el('div', {className: 'layout'});
  const tablesPanel = el('div', {className: 'panel tables-panel'});
  tablesPanel.appendChild(el('h2', {text: 'Your Tables'}));
  data.tables.forEach(function(name) {
    const btn = el('button', {className: 'table-item', text: name});
    btn.dataset.table = name;
    btn.addEventListener('click', function() { selectTable(name); });
    tablesPanel.appendChild(btn);
  });
  const detailPanel = el('div', {className: 'panel detail-panel'});
  detailPanel.id = 'detail';
  detailPanel.appendChild(el('div', {className: 'placeholder', text: 'Click a table to see its columns.'}));
  layout.appendChild(tablesPanel);
  layout.appendChild(detailPanel);
  content.appendChild(layout);
}

async function selectTable(name) {
  document.querySelectorAll('.table-item').forEach(function(node) {
    node.classList.toggle('selected', node.dataset.table === name);
  });
  const detail = document.getElementById('detail');
  clear(detail);
  detail.appendChild(el('div', {className: 'placeholder', text: 'Loading...'}));

  const resp = await fetch('/api/tables/' + encodeURIComponent(name) + '/columns');
  const data = await resp.json();
  clear(detail);
  if (!data.connected) {
    detail.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }
  if (!data.found) {
    detail.appendChild(el('div', {className: 'not-connected', text: 'No columns found for "' + name + '".'}));
    return;
  }

  detail.appendChild(el('h2', {text: 'Columns in "' + name + '"'}));
  const table = el('table', {className: 'col-table'});
  const headerRow = el('tr');
  ['Column', 'Type', 'Can be blank?'].forEach(function(h) { headerRow.appendChild(el('th', {text: h})); });
  table.appendChild(headerRow);
  data.columns.forEach(function(col) {
    const row = el('tr');
    row.appendChild(el('td', {text: col.name}));
    row.appendChild(el('td', {text: col.data_type}));
    row.appendChild(el('td', {text: col.nullable ? 'optional' : 'required'}));
    table.appendChild(row);
  });
  detail.appendChild(table);

  detail.appendChild(el('h2', {text: 'Does this match what our checks need?'}));
  const grid = el('div', {className: 'req-grid'});
  data.requirement_checks.forEach(function(check) {
    const card = el('div', {className: 'req-card ' + (check.satisfied ? 'satisfied' : 'unsatisfied')});
    const heading = el('strong', {text: check.label});
    card.appendChild(heading);
    card.appendChild(document.createTextNode(check.satisfied ? ' - has everything needed' : ' - missing something'));
    check.required.forEach(function(c) {
      const line = el('div', {className: 'req-line'});
      const dot = el('span', {className: 'dot ' + (c.present ? 'dot-yes' : 'dot-no')});
      line.appendChild(dot);
      line.appendChild(document.createTextNode(c.name + ' - ' + c.description));
      card.appendChild(line);
    });
    grid.appendChild(card);
  });
  detail.appendChild(grid);
}

loadTables();
</script>
</body>
</html>""")


def _requirement_check_to_dict(check: RequirementCheckResult) -> dict:
    return {
        "dataset_name": check.dataset_name,
        "label": check.label,
        "satisfied": check.satisfied,
        "required": [
            {"name": c.name, "description": c.description, "example": c.example, "present": c.present}
            for c in check.required
        ],
        "optional": [
            {"name": c.name, "description": c.description, "example": c.example, "present": c.present}
            for c in check.optional
        ],
    }


class DataConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # data_console's own JSON logger covers what's worth logging

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_html(200, _PAGE_TEMPLATE.substitute())
            return
        if self.path == "/api/tables":
            self._handle_list_tables()
            return
        match = _COLUMNS_PATH_RE.match(self.path)
        if match:
            self._handle_list_columns(unquote(match.group(1)))
            return
        self._send_json(404, {"error": "not found"})

    def _handle_list_tables(self) -> None:
        try:
            tables = schema_inspector.list_tables()
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return
        self._send_json(200, {"connected": True, "tables": tables})

    def _handle_list_columns(self, table_name: str) -> None:
        try:
            columns = schema_inspector.list_columns(table_name)
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return

        if not columns:
            self._send_json(200, {"connected": True, "found": False, "table": table_name})
            return

        column_names = [c.name for c in columns]
        requirement_checks = [
            _requirement_check_to_dict(check) for check in check_against_all_known_datasets(column_names)
        ]
        self._send_json(
            200,
            {
                "connected": True,
                "found": True,
                "table": table_name,
                "columns": [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in columns],
                "requirement_checks": requirement_checks,
            },
        )

    def _send_not_connected(self, message: str) -> None:
        logger.info(
            "data_console_not_connected",
            extra={"event": "data_console_not_connected", "outcome": "failure", "context": {"message": message}},
        )
        self._send_json(200, {"connected": False, "message": message})

    def _send_html(self, status: int, body_str: str) -> None:
        body = body_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(os.environ.get("SUPPLYMIND_DATA_CONSOLE_PORT", DEFAULT_PORT))
    server = HTTPServer(("127.0.0.1", port), DataConsoleHandler)
    print(f"Data console listening on http://127.0.0.1:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
