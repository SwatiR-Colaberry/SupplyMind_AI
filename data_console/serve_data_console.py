"""Local browser UI for connecting to, mapping, and browsing a database (data-console).

Covers Slices 1-4 of the agreed build order (same "one step at a time"
rhythm used for chat_interface/serve_chat_ui.py): Slice 1 connects to
Postgres and browses tables/columns against this repo's three known
required datasets; Slice 2 adds picking specific columns, joining a
second table, and a capped preview; Slice 3 adds "Map Your Data" - for
each of the 3 known datasets, point it at whichever table actually holds
that data even when its column names differ, using
data_integration/connection_profile.py's real mapping/validation engine,
plus a raw-SQL fallback for a dataset split across more tables than the
guided picker's single table can reach; Slice 4 adds uploading a CSV file
as a fourth way to map a dataset, for someone with no live database at
all. Google Sheets and zip-of-CSVs uploads remain later steps.

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

import datetime
import decimal
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template
from typing import Callable
from urllib.parse import unquote

from data_console import schema_inspector
from data_console.column_requirements import ALL_DATASETS, BY_NAME, ColumnRequirement, DatasetRequirements
from data_console.file_mapping_service import probe_upload_columns, save_mapping_from_file
from data_console.file_store import MAX_UPLOAD_BYTES, UnknownUploadError, UnsafeArchiveError, extract_csvs_from_zip, save_upload
from data_console.logging_setup import get_logger
from data_console.mapping_service import (
    clear_mapping,
    mark_unavailable,
    preview_mapping,
    probe_query_columns,
    save_mapping,
    save_mapping_from_query,
)
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.mapping_suggester import suggest_mapping
from data_console.preview_runner import run_preview
from data_console.query_builder import PREVIEW_ROW_LIMIT, InvalidSelectionError, JoinSpec, QuerySelection, SelectedColumn
from data_console.raw_query_validator import UnsafeQueryError
from data_console.runtime_db_config import set_runtime_config
from data_console.sheet_fetcher import InvalidSheetUrlError, SheetFetchError
from data_console.sheet_mapping_service import probe_sheet_columns, save_mapping_from_sheet
from data_integration.config import MissingConfigError, PostgresConfig
from data_integration.connection_profile import SchemaMappingError
from data_integration.postgres_connector import PostgresIntegrationError, fetch_rows

logger = get_logger()

DEFAULT_PORT = 8766

# Lets this server also serve the 3 static Executive Control Tower pages
# dashboard/run_sample_dashboard.py generates on disk (dashboard/control_
# tower_<scenario>.html), so "open data_console, click through to the
# dashboard, click back" is one browser tab on one origin instead of
# juggling separate file:// paths. A sibling top-level directory reached
# by relative filesystem path from this module's own __file__ - not an
# `import dashboard`, which would create the exact "A imports B imports
# A" this repo's Modular Composition Rule forbids: dashboard/run_sample_
# dashboard.py already imports from data_console (MappingStore,
# preview_mapping, ...), so data_console importing dashboard back would
# be a live cycle, not just style. The scenario/label pair is duplicated
# from dashboard/run_sample_dashboard.py's own _SCENARIO_LABELS for the
# same reason (see forecasting/aggregation.py's _FALLBACK_DATE_FORMATS
# for the earlier instance of this same tradeoff in this repo).
_DASHBOARD_HTML_DIR = Path(__file__).resolve().parent.parent / "dashboard"
_DASHBOARD_SCENARIO_LABELS: dict[str, str] = {
    "real_data": "Live Data",
    "partial_failure": "Demo: Partial Data",
    "synthetic_healthy": "Demo: Healthy Example",
}
# Served filename -> allowed. Checked by exact match before ever touching
# the filesystem, so a request path can never be turned into an arbitrary
# file read (Security Enforcement Layer: untrusted request input is never
# used to build a path/query without validation first).
_DASHBOARD_FILENAMES: frozenset[str] = frozenset(
    f"control_tower_{scenario}.html" for scenario in _DASHBOARD_SCENARIO_LABELS
)

# A selection with a handful of columns and one join is nowhere near this
# size; the cap exists purely so a malformed/absurd Content-Length can
# never make this single-threaded dev server misbehave, same reasoning
# and same value as chat_interface/serve_chat_ui.py's own constant.
MAX_REQUEST_BODY_BYTES = 65536

_COLUMNS_PATH_RE = re.compile(r"^/api/tables/([^/]+)/columns$")
_MAPPING_PATH_RE = re.compile(r"^/api/mappings/([^/]+)$")
_MAPPING_UNAVAILABLE_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/unavailable$")
_MAPPING_PREVIEW_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/preview$")
_MAPPING_FROM_QUERY_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/from-query$")
_MAPPING_FROM_FILE_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/from-file$")
_MAPPING_FROM_SHEET_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/from-sheet$")


def _json_default(value):
    """Postgres numeric/date columns come back from psycopg2 as Decimal/date/datetime,
    none of which json.dumps() can serialize on its own - real preview rows would
    otherwise crash this handler the moment a table had a numeric or date column,
    which is most of them. Converted to the same plain types every other JSON
    boundary in this repo already uses (a float, an ISO-8601 string)."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Data Console</title>
<style>
  :root {
    --ink: #131826;
    --ink-soft: #4a5468;
    --ink-faint: #8891a1;
    --surface: #ffffff;
    --canvas: #f2f4f8;
    --border: #e1e5ec;
    --border-soft: #edeff3;
    --brand: #1d4ed8;
    --brand-dark: #1638a6;
    --brand-tint: #eaf0fd;
    --success: #0f7b52;
    --success-tint: #e6f4ec;
    --warning: #a85c00;
    --warning-tint: #fbf0dd;
    --neutral: #5f6673;
    --neutral-tint: #eef0f3;
    --danger: #9a2b1e;
    --danger-tint: #fdecea;
    --danger-border: #f3c6c1;
    --shadow: 0 1px 2px rgba(19, 24, 38, 0.05), 0 1px 8px rgba(19, 24, 38, 0.04);
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--canvas); color: var(--ink); margin: 0;
    -webkit-font-smoothing: antialiased;
  }
  .page { max-width: 1180px; margin: 0 auto; padding: 32px 32px 56px; }
  .kicker {
    display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--brand); background: var(--brand-tint); padding: 3px 10px; border-radius: 100px; margin-bottom: 10px;
  }
  h1 { font-size: 25px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 4px; }
  h2 {
    font-size: 12px; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-soft); margin: 0 0 12px;
  }
  h3 { font-size: 15px; font-weight: 600; margin: 0; color: var(--ink); }
  .topbar { margin-bottom: 24px; }
  .nav-bar {
    display: flex; gap: 6px; margin-bottom: 16px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 5px; box-shadow: var(--shadow); width: fit-content;
  }
  .nav-item {
    font-size: 12.5px; font-weight: 600; padding: 7px 14px; border-radius: 7px; text-decoration: none;
    color: var(--ink-soft);
  }
  a.nav-item:hover { background: var(--brand-tint); color: var(--brand-dark); }
  .nav-current { background: var(--brand); color: white; }
  .meta { color: var(--ink-soft); font-size: 14px; margin-bottom: 20px; }
  .intro {
    background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--brand); border-radius: 10px;
    padding: 16px 18px; margin-bottom: 28px; font-size: 13.5px; line-height: 1.55; color: var(--ink-soft);
  }
  .connect-section { margin-bottom: 32px; }
  .connect-card {
    background: linear-gradient(165deg, var(--brand-tint) 0%, var(--surface) 55%);
    border: 1px solid var(--border); border-radius: 12px; padding: 22px 24px; box-shadow: var(--shadow);
  }
  .connect-intro { font-size: 13.5px; color: var(--ink-soft); margin-bottom: 18px; max-width: 640px; line-height: 1.5; }
  .connect-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .connect-option {
    display: flex; flex-direction: column; align-items: flex-start; gap: 8px; text-align: left;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 16px 18px;
    cursor: pointer; font-family: inherit; transition: transform 0.12s ease, box-shadow 0.12s ease, border-color 0.12s ease;
  }
  .connect-option:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(19,24,38,0.09); border-color: #c7cde0; }
  .connect-option .connect-icon {
    width: 34px; height: 34px; border-radius: 9px; background: var(--brand-tint); color: var(--brand);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .connect-option .connect-icon svg { width: 18px; height: 18px; }
  .connect-option-title { font-size: 13.5px; font-weight: 650; color: var(--ink); }
  .connect-option-desc { font-size: 12px; color: var(--ink-faint); line-height: 1.45; }
  .connect-status-card {
    display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
    background: var(--success-tint); border: 1px solid #bfe3cf; border-radius: 12px; padding: 16px 20px;
  }
  .connect-status-left { display: flex; align-items: center; gap: 12px; }
  .connect-status-icon {
    width: 32px; height: 32px; border-radius: 100px; background: var(--success); color: white;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .connect-status-icon svg { width: 16px; height: 16px; }
  .connect-status-title { font-size: 13.5px; font-weight: 650; color: var(--ink); }
  .connect-status-desc { font-size: 12.5px; color: var(--ink-soft); }
  .layout { display: flex; gap: 16px; align-items: flex-start; }
  .panel {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 18px; box-shadow: var(--shadow);
  }
  .tables-panel { width: 240px; flex-shrink: 0; }
  .detail-panel { flex: 1; min-width: 0; }
  .table-item {
    display: block; width: 100%; text-align: left; padding: 8px 10px; border: none;
    background: none; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--ink);
  }
  .table-item:hover { background: var(--brand-tint); }
  .table-item.selected { background: var(--brand-tint); color: var(--brand-dark); font-weight: 600; }
  .placeholder { color: var(--ink-faint); font-size: 13px; }
  .not-connected {
    background: var(--warning-tint); border: 1px solid #f0d8ab; color: #7a4600;
    padding: 12px 14px; border-radius: 8px; font-size: 13px; line-height: 1.5;
  }
  .not-connected code { background: rgba(0,0,0,0.06); padding: 1px 5px; border-radius: 4px; }
  table.col-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-bottom: 16px; }
  table.col-table th {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
    color: var(--ink-soft); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.03em;
  }
  table.col-table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border-soft); }
  .select-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border); }
  .col-checkbox-row { display: flex; align-items: center; gap: 7px; font-size: 12.5px; padding: 4px 0; }
  .btn {
    padding: 7px 14px; border: 1px solid var(--border); background: var(--surface); color: var(--ink);
    border-radius: 7px; font-size: 12.5px; font-weight: 550; cursor: pointer; margin: 4px 6px 4px 0;
    transition: border-color 0.12s ease, background 0.12s ease;
  }
  .btn:hover { border-color: #c7cde0; background: #f8f9fc; }
  .btn-primary { background: var(--brand); border-color: var(--brand); color: white; }
  .btn-primary:hover { background: var(--brand-dark); border-color: var(--brand-dark); }
  .join-block { margin: 10px 0; padding: 12px 14px; background: var(--canvas); border: 1px solid var(--border-soft); border-radius: 8px; }
  .join-row { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin-bottom: 8px; flex-wrap: wrap; }
  select {
    padding: 5px 9px; border: 1px solid var(--border); border-radius: 6px; font-size: 12.5px;
    background: var(--surface); color: var(--ink);
  }
  .sql-box {
    background: #10152a; color: #c9d8ff; padding: 12px 14px; border-radius: 8px; font-size: 11.5px;
    overflow-x: auto; margin: 10px 0; white-space: pre-wrap; word-break: break-word; line-height: 1.5;
  }
  .truncated-note { font-size: 11.5px; color: var(--ink-faint); margin-top: 6px; }
  .error-box {
    background: var(--danger-tint); border: 1px solid var(--danger-border); color: var(--danger);
    padding: 9px 13px; border-radius: 8px; font-size: 12.5px; margin: 10px 0; line-height: 1.45;
  }
  .mapping-section { margin-bottom: 28px; }
  .mapping-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 14px; }
  .mapping-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 18px; box-shadow: var(--shadow); transition: box-shadow 0.15s ease, border-color 0.15s ease;
  }
  .mapping-card:hover { border-color: #c7cde0; box-shadow: 0 4px 14px rgba(19,24,38,0.07); }
  .mapping-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
  .status-badge {
    flex-shrink: 0; font-size: 10.5px; font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase;
    padding: 3px 9px; border-radius: 100px; white-space: nowrap;
  }
  .status-mapped .status-badge { background: var(--success-tint); color: var(--success); }
  .status-not_mapped .status-badge { background: var(--warning-tint); color: var(--warning); }
  .status-unavailable .status-badge { background: var(--neutral-tint); color: var(--neutral); }
  .mapping-status-line { font-size: 12.5px; color: var(--ink-soft); margin-bottom: 10px; }
  .mapping-unavailable-note { font-size: 12px; color: var(--warning); background: var(--warning-tint); border-radius: 7px; padding: 7px 10px; margin: -2px 0 12px; }
  .mapping-computed-note { font-size: 12px; color: var(--brand-dark); background: var(--brand-tint); border-radius: 7px; padding: 7px 10px; margin: -2px 0 12px; }
  .mapping-fields { font-size: 12.5px; color: var(--ink-soft); margin-bottom: 12px; line-height: 1.6; }
  .mapping-fields .field-line { margin: 2px 0; }
  .mapping-fields .field-optional { color: var(--ink-faint); }
  .search-input {
    width: 100%; padding: 7px 11px; border: 1px solid var(--border); border-radius: 7px; font-size: 12.5px;
    margin-bottom: 8px; box-sizing: border-box; background: var(--surface); color: var(--ink);
  }
  .search-input:focus, .sql-input:focus, select:focus { outline: 2px solid var(--brand-tint); border-color: var(--brand); }
  .table-search-results { max-height: 160px; overflow-y: auto; border: 1px solid var(--border-soft); border-radius: 7px; margin-bottom: 8px; }
  .mapping-form-row { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin: 7px 0; flex-wrap: wrap; }
  .mapping-form-row > label:first-child { width: 150px; flex-shrink: 0; color: var(--ink-soft); }
  .mapping-form-row select, .mapping-form-row input[type="text"], .mapping-form-row input[type="password"] { flex: 1; min-width: 0; max-width: 100%; margin-bottom: 0; }
  .mapping-form-row label.unavailable-check {
    display: flex; align-items: center; gap: 5px; width: auto; flex: none; color: var(--ink-faint); font-size: 12px; white-space: nowrap; cursor: pointer;
  }
  .mapping-form-row label.unavailable-check input[type="checkbox"] { flex: none; margin: 0; }
  .mapping-compute-row {
    flex-direction: column; align-items: stretch; gap: 3px; margin: -2px 0 7px; padding: 8px 10px;
    background: var(--neutral-tint); border-radius: 7px;
  }
  .mapping-compute-row > label, .mapping-compute-row > label:first-child {
    width: auto; flex-shrink: 0; color: var(--ink-faint); font-size: 11.5px; margin-top: 4px;
  }
  .mapping-compute-row > label:first-child { margin-top: 0; }
  .mapping-compute-row select { width: 100%; }
  .table-scroll { overflow-x: auto; max-width: 100%; }
  .sql-input {
    width: 100%; padding: 9px 11px; border: 1px solid var(--border); border-radius: 7px; font-size: 12.5px;
    margin-bottom: 8px; box-sizing: border-box; font-family: "SF Mono", Menlo, Consolas, monospace; color: var(--ink);
  }
  .btn-link {
    background: none; border: none; color: var(--brand); text-decoration: none; font-size: 12.5px;
    font-weight: 550; cursor: pointer; padding: 5px 0; display: block;
  }
  .btn-link:hover { text-decoration: underline; color: var(--brand-dark); }
  .guardrail-note { font-size: 11.5px; color: var(--ink-faint); margin: 4px 0 10px; line-height: 1.5; }
</style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <div class="nav-bar">
      <span class="nav-item nav-current">Data Console</span>
      <a class="nav-item" href="/dashboard/control_tower_real_data.html">Live Data</a>
      <a class="nav-item" href="/dashboard/control_tower_partial_failure.html">Demo: Partial Data</a>
      <a class="nav-item" href="/dashboard/control_tower_synthetic_healthy.html">Demo: Healthy Example</a>
    </div>
    <div class="kicker">Supply Chain Data</div>
    <h1>Data Console</h1>
    <div class="meta">Connect your data once, then map it to the datasets this system needs for analysis.</div>
  </div>
  <div class="intro">
    This page reads directly from your data — it never modifies or moves anything. Connect a database,
    a CSV file, or a Google Sheet once below, then map each of the three datasets this system needs
    (Customer Orders, Inventory, and Delivery Records) to it, even if the column names differ from ours.
    You can also browse any table directly, select specific columns, join a second table, and preview
    the results. Every preview is limited to $row_limit rows so this page never attempts to load your
    entire database at once.
  </div>
  <div class="connect-section" id="connect-section">
    <h2>Connect Your Data</h2>
    <div class="placeholder">Loading...</div>
  </div>
  <div class="mapping-section" id="mapping-section">
    <h2>Map Your Data</h2>
    <div class="placeholder">Loading...</div>
  </div>
  <div id="content">
    <div class="placeholder">Loading...</div>
  </div>
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
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, opts) {
  const node = document.createElement(tag);
  opts = opts || {};
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = opts.text;
  return node;
}

// State for the "Build a selection" section below the browse view. Reset
// every time a different base table is picked. The checkboxes/selects
// themselves are the source of truth for what's checked at Preview time
// (read via querySelector at that moment) - these just track which
// tables/columns are currently on screen so the join picker knows what
// options to offer.
let allTables = [];
let baseTable = null;
let baseColumns = [];
let joinTableColumns = [];

function buildRowsTable(rows) {
  if (rows.length === 0) {
    return el('div', {className: 'placeholder', text: 'This matched 0 rows.'});
  }
  const columnNames = Object.keys(rows[0]);
  const table = el('table', {className: 'col-table'});
  const headerRow = el('tr');
  columnNames.forEach(function(name) { headerRow.appendChild(el('th', {text: name})); });
  table.appendChild(headerRow);
  rows.forEach(function(row) {
    const tr = el('tr');
    columnNames.forEach(function(name) {
      const value = row[name];
      tr.appendChild(el('td', {text: value === null || value === undefined ? '' : String(value)}));
    });
    table.appendChild(tr);
  });
  // A row wide enough to overflow its container (a narrow mapping card,
  // or many joined columns) scrolls horizontally within this wrapper
  // instead of clipping silently or blowing out the page's own layout.
  const wrapper = el('div', {className: 'table-scroll'});
  wrapper.appendChild(table);
  return wrapper;
}

function buildColumnCheckboxes(tableName, columns, defaultChecked) {
  const container = el('div');
  columns.forEach(function(col) {
    // <label> wrapping both the checkbox and its text, rather than a
    // separate <div>+<label for="...">, so clicking anywhere on the row
    // toggles it without needing to invent unique element ids per checkbox.
    const row = el('label', {className: 'col-checkbox-row'});
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'col-checkbox';
    checkbox.checked = defaultChecked;
    checkbox.dataset.table = tableName;
    checkbox.dataset.column = col.name;
    row.appendChild(checkbox);
    row.appendChild(document.createTextNode(col.name + ' (' + col.data_type + ')'));
    container.appendChild(row);
  });
  return container;
}

async function loadTables() {
  const content = document.getElementById('content');
  const resp = await fetch('/api/tables');
  const data = await resp.json();
  clear(content);
  if (!data.connected) {
    // Deliberately not styled as an error: showing up disconnected is this
    // page's normal starting state, not a problem to alarm someone with the
    // moment they open it - a real error box only appears below in response
    // to an actual attempt to connect that actually failed.
    content.appendChild(el('div', {
      className: 'placeholder',
      text: 'Browse any table directly once a database is connected. If you just want to map Customer Orders, Inventory, or Delivery Records, you can do that above with a CSV file or a Google Sheets link instead — no database required.',
    }));
    const dbBtn = el('button', {className: 'btn btn-primary', text: 'Connect a Database'});
    dbBtn.addEventListener('click', function() {
      renderDbConnectionForm(content, function() { loadTables(); }, function() { return loadTables(); });
    });
    content.appendChild(dbBtn);
    return;
  }
  if (data.tables.length === 0) {
    content.appendChild(el('div', {className: 'not-connected', text: 'Connected, but no tables were found in this database yet.'}));
    return;
  }
  allTables = data.tables;

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
  detailPanel.appendChild(el('div', {className: 'placeholder', text: 'Select a table to view its columns.'}));
  layout.appendChild(tablesPanel);
  layout.appendChild(detailPanel);
  content.appendChild(layout);
}

async function fetchColumns(tableName) {
  const resp = await fetch('/api/tables/' + encodeURIComponent(tableName) + '/columns');
  return resp.json();
}

async function selectTable(name) {
  document.querySelectorAll('.table-item').forEach(function(node) {
    node.classList.toggle('selected', node.dataset.table === name);
  });
  const detail = document.getElementById('detail');
  clear(detail);
  detail.appendChild(el('div', {className: 'placeholder', text: 'Loading...'}));

  const data = await fetchColumns(name);
  clear(detail);
  if (!data.connected) {
    detail.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }
  if (!data.found) {
    detail.appendChild(el('div', {className: 'not-connected', text: 'No columns found for "' + name + '".'}));
    return;
  }

  baseTable = name;
  baseColumns = data.columns;
  joinTableColumns = [];

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

  detail.appendChild(buildSelectSection(name, data.columns));
}

function buildSelectSection(tableName, columns) {
  const section = el('div', {className: 'select-section'});
  section.id = 'select-section';

  section.appendChild(el('h2', {text: 'Build a Selection'}));
  section.appendChild(el('div', {className: 'placeholder', text: 'Choose which columns to include from "' + tableName + '" (all are selected by default). Join a second table if the data you need is split across two.'}));
  section.appendChild(buildColumnCheckboxes(tableName, columns, true));

  const joinArea = el('div');
  joinArea.id = 'join-area';
  const addJoinBtn = el('button', {className: 'btn', text: '+ Join Another Table'});
  addJoinBtn.addEventListener('click', function() { showJoinPicker(joinArea); });
  joinArea.appendChild(addJoinBtn);
  section.appendChild(joinArea);

  const previewBtn = el('button', {className: 'btn btn-primary', text: 'Preview'});
  previewBtn.addEventListener('click', runPreview);
  section.appendChild(previewBtn);

  const resultArea = el('div');
  resultArea.id = 'preview-result';
  section.appendChild(resultArea);

  return section;
}

async function showJoinPicker(joinArea) {
  clear(joinArea);
  const otherTables = allTables.filter(function(t) { return t !== baseTable; });
  if (otherTables.length === 0) {
    joinArea.appendChild(el('div', {className: 'placeholder', text: 'No other tables are available to join.'}));
    return;
  }

  const select = document.createElement('select');
  const placeholder = el('option', {text: 'Choose a table to join...'});
  placeholder.value = '';
  select.appendChild(placeholder);
  otherTables.forEach(function(t) {
    const opt = el('option', {text: t});
    opt.value = t;
    select.appendChild(opt);
  });
  joinArea.appendChild(select);

  const joinDetail = el('div');
  joinDetail.id = 'join-detail';
  joinArea.appendChild(joinDetail);

  select.addEventListener('change', async function() {
    if (!select.value) { clear(joinDetail); joinTableColumns = []; return; }
    clear(joinDetail);
    joinDetail.appendChild(el('div', {className: 'placeholder', text: 'Loading...'}));
    const data = await fetchColumns(select.value);
    clear(joinDetail);
    if (!data.connected || !data.found) {
      joinDetail.appendChild(el('div', {className: 'not-connected', text: data.message || 'Could not load that table.'}));
      return;
    }
    joinTableColumns = data.columns;
    renderJoinDetail(joinDetail, select.value, data.columns);
  });
}

function renderJoinDetail(joinDetail, joinTableName, columns) {
  const block = el('div', {className: 'join-block'});

  const joinRow = el('div', {className: 'join-row'});
  joinRow.appendChild(document.createTextNode('Match a column in ' + baseTable));
  const leftSelect = document.createElement('select');
  leftSelect.id = 'join-left-column';
  baseColumns.forEach(function(c) {
    const opt = el('option', {text: c.name});
    opt.value = c.name;
    leftSelect.appendChild(opt);
  });
  joinRow.appendChild(leftSelect);

  joinRow.appendChild(document.createTextNode('to a column in ' + joinTableName));
  const rightSelect = document.createElement('select');
  rightSelect.id = 'join-right-column';
  columns.forEach(function(c) {
    const opt = el('option', {text: c.name});
    opt.value = c.name;
    rightSelect.appendChild(opt);
  });
  joinRow.appendChild(rightSelect);
  block.appendChild(joinRow);

  // If a column with the same name exists on both sides, preselect it -
  // a helpful guess, not a requirement; either dropdown can be changed.
  const baseNames = baseColumns.map(function(c) { return c.name; });
  const matching = columns.map(function(c) { return c.name; }).find(function(n) { return baseNames.indexOf(n) !== -1; });
  if (matching) {
    leftSelect.value = matching;
    rightSelect.value = matching;
  }

  block.dataset.joinTable = joinTableName;
  block.appendChild(el('div', {className: 'placeholder', text: 'Columns from "' + joinTableName + '" to include'}));
  block.appendChild(buildColumnCheckboxes(joinTableName, columns, false));

  joinDetail.appendChild(block);
}

async function runPreview() {
  const resultArea = document.getElementById('preview-result');
  clear(resultArea);
  resultArea.appendChild(el('div', {className: 'placeholder', text: 'Running preview...'}));

  const columns = [];
  document.querySelectorAll('.col-checkbox:checked').forEach(function(cb) {
    columns.push({table: cb.dataset.table, column: cb.dataset.column});
  });

  let join = null;
  const joinBlock = document.querySelector('.join-block');
  const leftSelect = document.getElementById('join-left-column');
  const rightSelect = document.getElementById('join-right-column');
  if (joinBlock && leftSelect && rightSelect && leftSelect.value && rightSelect.value) {
    join = {
      left_table: baseTable,
      left_column: leftSelect.value,
      right_table: joinBlock.dataset.joinTable,
      right_column: rightSelect.value,
    };
  }

  const resp = await fetch('/api/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({base_table: baseTable, columns: columns, join: join}),
  });
  const data = await resp.json();
  clear(resultArea);

  // A 400 (invalid selection) body has no "connected" key at all - only a
  // 200 "connected: false" (a real not-connected/unreachable-database
  // state) carries the plain-language `message`. Checking `error` first
  // avoids rendering "undefined" in a not-connected box for a 400.
  if (data.error) {
    resultArea.appendChild(el('div', {className: 'error-box', text: data.error}));
    return;
  }
  if (!data.connected) {
    resultArea.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }

  resultArea.appendChild(el('h2', {text: 'Preview'}));
  const sqlBox = el('pre', {className: 'sql-box', text: data.sql});
  resultArea.appendChild(sqlBox);

  resultArea.appendChild(buildRowsTable(data.rows));
  if (data.truncated) {
    resultArea.appendChild(el('div', {className: 'truncated-note', text: 'Showing the first ' + data.row_count + ' rows. There may be more.'}));
  }
}

// --- Map Your Data (Slice 3) ---
// Turns "browse hundreds of tables" into "fill in 3 slots": for each of
// the 3 datasets this system knows about, either point it at a table
// (mapping differently-named columns to the standard names) or say
// plainly "this data isn't in this database." Every mapping is
// validated against the real live schema (data_integration/
// connection_profile.py's validate_profile()) before it's ever saved -
// nothing here decides a mapping is correct on its own, it only helps
// fill in the form and calls the same engine that does.

let datasetRequirements = [];
let currentMappings = {};

function buildFieldsList(requirements) {
  const container = el('div', {className: 'mapping-fields'});
  requirements.required.forEach(function(f) {
    const line = el('div', {className: 'field-line'});
    line.appendChild(document.createTextNode(f.name + ' (required): ' + f.description + ' — for example, "' + f.example + '".'));
    container.appendChild(line);
  });
  requirements.optional.forEach(function(f) {
    const line = el('div', {className: 'field-line field-optional'});
    line.appendChild(document.createTextNode(f.name + ' (optional): ' + f.description + ' — for example, "' + f.example + '".'));
    container.appendChild(line);
  });
  return container;
}

function mappingStatusLine(status) {
  if (status.status === 'mapped') {
    if (status.source_kind === 'query') return 'Mapped via a custom query';
    if (status.source_kind === 'file') return 'Mapped from uploaded file "' + status.filename + '"';
    if (status.source_kind === 'sheet') return 'Mapped from a Google Sheet link';
    return 'Mapped to "' + status.table + '"';
  }
  if (status.status === 'unavailable') return 'Marked as not available in this database';
  return 'Not yet mapped';
}

function statusBadgeText(status) {
  if (status.status === 'mapped') return 'Mapped';
  if (status.status === 'unavailable') return 'Unavailable';
  return 'Not Mapped';
}

async function loadMappingSection() {
  const section = document.getElementById('mapping-section');
  const [datasetsResp, mappingsResp] = await Promise.all([fetch('/api/datasets'), fetch('/api/mappings')]);
  const datasetsData = await datasetsResp.json();
  const mappingsData = await mappingsResp.json();
  datasetRequirements = datasetsData.datasets;
  currentMappings = mappingsData.mappings;
  renderMappingCards();
}

function renderMappingCards() {
  const section = document.getElementById('mapping-section');
  clear(section);
  section.appendChild(el('h2', {text: 'Map Your Data'}));

  const grid = el('div', {className: 'mapping-cards'});
  datasetRequirements.forEach(function(requirements) {
    const status = currentMappings[requirements.dataset_name] || {status: 'not_mapped'};
    const card = el('div', {className: 'mapping-card status-' + status.status});
    const head = el('div', {className: 'mapping-card-head'});
    head.appendChild(el('h3', {text: requirements.label}));
    head.appendChild(el('span', {className: 'status-badge', text: statusBadgeText(status)}));
    card.appendChild(head);
    card.appendChild(el('div', {className: 'mapping-status-line', text: mappingStatusLine(status)}));
    if (status.status === 'mapped' && status.unavailable_fields && status.unavailable_fields.length > 0) {
      card.appendChild(el('div', {
        className: 'mapping-unavailable-note',
        text: 'Not available in this dataset: ' + status.unavailable_fields.join(', ') + ' — any analysis needing these will be skipped.',
      }));
    }
    if (status.status === 'mapped' && status.computed_date_fields && Object.keys(status.computed_date_fields).length > 0) {
      const computedDescriptions = Object.keys(status.computed_date_fields).map(function(fieldName) {
        const spec = status.computed_date_fields[fieldName];
        return fieldName + ' = "' + spec.base_date_column + '" + "' + spec.offset_days_column + '" day(s)';
      });
      card.appendChild(el('div', {
        className: 'mapping-computed-note',
        text: 'Computed: ' + computedDescriptions.join('; ') + '.',
      }));
    }
    card.appendChild(buildFieldsList(requirements));

    const actions = el('div');
    if (status.status === 'mapped') {
      const previewBtn = el('button', {className: 'btn', text: 'Preview'});
      previewBtn.addEventListener('click', function() { previewMapping(requirements.dataset_name); });
      const changeBtn = el('button', {className: 'btn', text: 'Change'});
      changeBtn.addEventListener('click', function() { startMapping(requirements); });
      const clearBtn = el('button', {className: 'btn', text: 'Clear'});
      clearBtn.addEventListener('click', function() { clearMapping(requirements.dataset_name); });
      actions.appendChild(previewBtn);
      actions.appendChild(changeBtn);
      actions.appendChild(clearBtn);
    } else {
      const mapBtn = el('button', {className: 'btn btn-primary', text: 'Map This Dataset'});
      mapBtn.addEventListener('click', function() { startMapping(requirements); });
      actions.appendChild(mapBtn);
      if (status.status === 'unavailable') {
        const clearBtn = el('button', {className: 'btn', text: 'Undo "Not Available"'});
        clearBtn.addEventListener('click', function() { clearMapping(requirements.dataset_name); });
        actions.appendChild(clearBtn);
      } else {
        const unavailableBtn = el('button', {className: 'btn', text: 'Not Available in This Database'});
        unavailableBtn.addEventListener('click', function() { markUnavailable(requirements.dataset_name); });
        actions.appendChild(unavailableBtn);
      }
    }
    card.appendChild(actions);

    const pickerArea = el('div');
    pickerArea.id = 'mapping-picker-' + requirements.dataset_name;
    card.appendChild(pickerArea);

    const resultArea = el('div');
    resultArea.id = 'mapping-result-' + requirements.dataset_name;
    card.appendChild(resultArea);

    grid.appendChild(card);
  });
  section.appendChild(grid);
}

async function startMapping(requirements) {
  const pickerArea = document.getElementById('mapping-picker-' + requirements.dataset_name);
  clear(pickerArea);

  // A source already connected via "Connect Your Data" at the top of the
  // page - jump straight to mapping from it instead of asking again.
  if (activeSourceKind) {
    await renderFromActiveSource(requirements, pickerArea);
    return;
  }

  pickerArea.appendChild(el('div', {className: 'placeholder', text: 'Checking for a connected database...'}));

  const resp = await fetch('/api/tables');
  const data = await resp.json();
  clear(pickerArea);

  if (!data.connected) {
    renderSourceChooser(requirements, pickerArea, data.message);
    return;
  }

  activeSourceKind = 'database';
  renderTablePicker(requirements, pickerArea, data.tables);
}

// Once a source has been connected once at the top of the page, every
// dataset card jumps straight to mapping from it - the same full chooser
// (renderSourceChooser) is still one click away via "Use a different
// source," for the real case where one dataset genuinely lives somewhere
// else than the other two.
async function renderFromActiveSource(requirements, pickerArea) {
  function appendOverrideLink() {
    const link = el('button', {className: 'btn-link', text: 'Use a different source for this dataset'});
    link.addEventListener('click', function() { renderSourceChooser(requirements, pickerArea, null); });
    pickerArea.appendChild(link);
  }

  if (activeSourceKind === 'file' && lastFileUpload) {
    renderFileMappingForm(requirements, lastFileUpload.fileId, lastFileUpload.filename, lastFileUpload.columns, lastFileUpload.suggestedMappings, pickerArea);
    appendOverrideLink();
    return;
  }
  if (activeSourceKind === 'zip' && lastZipUpload) {
    renderZipMemberPicker(requirements, lastZipUpload.filename, lastZipUpload.members, pickerArea);
    appendOverrideLink();
    return;
  }
  if (activeSourceKind === 'sheet' && lastSheetLink) {
    await probeAndRenderSheetForm(requirements, lastSheetLink.url, pickerArea);
    appendOverrideLink();
    return;
  }

  // 'database', or a stale/cleared reference to a file/zip that no longer
  // matches its cache (e.g. "Change" was clicked at the top since) - fall
  // back to a fresh connection check the same way the no-source path does.
  pickerArea.appendChild(el('div', {className: 'placeholder', text: 'Checking for a connected database...'}));
  const resp = await fetch('/api/tables');
  const data = await resp.json();
  clear(pickerArea);
  if (!data.connected) {
    renderSourceChooser(requirements, pickerArea, data.message);
    return;
  }
  // renderTablePicker already offers its own query/upload/Sheets
  // alternatives inline, so no separate override link is needed here -
  // unlike the file/zip/sheet cases above, which are single-purpose views.
  renderTablePicker(requirements, pickerArea, data.tables);
}

// A single CSV or zip uploaded while mapping a different dataset already
// sits on disk - reuse it here instead of re-uploading the same (possibly
// large) file again for a dataset whose columns happen to live in that
// same file or bundle. Shared by the not-connected chooser and the
// connected table picker, since either can be the first place a person
// lands.
function appendReuseUploadLinks(requirements, pickerArea) {
  if (lastFileUpload) {
    const orFileBtn = el('button', {
      className: 'btn-link',
      text: 'Or reuse the previously uploaded file ("' + lastFileUpload.filename + '")',
    });
    orFileBtn.addEventListener('click', function() {
      clear(pickerArea);
      const statusArea = el('div');
      pickerArea.appendChild(statusArea);
      renderFileMappingForm(
        requirements, lastFileUpload.fileId, lastFileUpload.filename,
        lastFileUpload.columns, lastFileUpload.suggestedMappings, statusArea
      );
    });
    pickerArea.appendChild(orFileBtn);
  }
  if (lastZipUpload) {
    const orZipBtn = el('button', {
      className: 'btn-link',
      text: 'Or choose a file from the previously uploaded archive ("' + lastZipUpload.filename + '")',
    });
    orZipBtn.addEventListener('click', function() {
      clear(pickerArea);
      const statusArea = el('div');
      pickerArea.appendChild(statusArea);
      renderZipMemberPicker(requirements, lastZipUpload.filename, lastZipUpload.members, statusArea);
    });
    pickerArea.appendChild(orZipBtn);
  }
}

// The entry point when no database is connected yet: an explicit choice
// between the three ways to provide this dataset's data, rather than a
// disabled search box with an error message above a stack of easy-to-miss
// links.
function renderSourceChooser(requirements, pickerArea, notConnectedMessage) {
  clear(pickerArea);
  pickerArea.appendChild(el('div', {
    className: 'placeholder',
    text: 'How would you like to provide data for ' + requirements.label + '?',
  }));

  const dbBtn = el('button', {className: 'btn btn-primary', text: 'Connect a Database'});
  dbBtn.addEventListener('click', function() {
    renderDbConnectionForm(
      pickerArea,
      function() { renderSourceChooser(requirements, pickerArea, notConnectedMessage); },
      function() { return startMapping(requirements); }
    );
  });
  pickerArea.appendChild(dbBtn);

  const uploadBtn = el('button', {className: 'btn', text: 'Upload a CSV or ZIP File'});
  uploadBtn.addEventListener('click', function() { uploadFileForMapping(requirements, pickerArea); });
  pickerArea.appendChild(uploadBtn);

  const sheetBtn = el('button', {className: 'btn', text: 'Connect a Google Sheet'});
  sheetBtn.addEventListener('click', function() { pasteSheetLinkForMapping(requirements, pickerArea); });
  pickerArea.appendChild(sheetBtn);

  appendReuseUploadLinks(requirements, pickerArea);

  const cancelBtn = el('button', {className: 'btn', text: 'Cancel'});
  cancelBtn.addEventListener('click', function() { clear(pickerArea); });
  pickerArea.appendChild(cancelBtn);
}

// The connection form for "Connect a Database" - tested for real against
// the submitted details before this session's runtime override is set, so
// a typo never leaves the tool pointed at a connection that doesn't work.
// The password is only ever sent once, over this one POST, to this same
// local server; it is never written to disk or echoed back. `container` is
// the DOM node to render into; `onBack()` returns to whatever view offered
// this form; `onConnected()` runs after a successful connect (so a
// per-dataset picker can resume mapping, or the top-level table browser
// can reload its table list).
function renderDbConnectionForm(container, onBack, onConnected) {
  clear(container);
  container.appendChild(el('div', {className: 'placeholder', text: 'Enter your database connection details.'}));

  function field(labelText, type, placeholder) {
    const row = el('div', {className: 'mapping-form-row'});
    row.appendChild(el('label', {text: labelText}));
    const input = document.createElement('input');
    input.type = type;
    input.className = 'search-input';
    if (placeholder) input.placeholder = placeholder;
    row.appendChild(input);
    container.appendChild(row);
    return input;
  }

  const hostInput = field('Host *', 'text', 'localhost');
  const portInput = field('Port', 'text', '5432');
  const databaseInput = field('Database *', 'text', 'supplymind');
  const userInput = field('User *', 'text', 'postgres');
  const passwordInput = field('Password *', 'password');

  const connectBtn = el('button', {className: 'btn btn-primary', text: 'Connect'});
  const backBtn = el('button', {className: 'btn', text: 'Back'});
  backBtn.addEventListener('click', onBack);
  container.appendChild(connectBtn);
  container.appendChild(backBtn);

  container.appendChild(el('div', {
    className: 'guardrail-note',
    text: 'These details are kept in memory for this session only - never written to disk. ' +
      '(Alternative: set SUPPLYMIND_PG_HOST and the other SUPPLYMIND_PG_* environment variables and restart instead.)',
  }));

  const statusArea = el('div');
  container.appendChild(statusArea);

  connectBtn.addEventListener('click', async function() {
    clear(statusArea);
    if (!hostInput.value.trim() || !databaseInput.value.trim() || !userInput.value.trim() || !passwordInput.value) {
      statusArea.appendChild(el('div', {className: 'error-box', text: 'Host, database, user, and password are all required.'}));
      return;
    }

    statusArea.appendChild(el('div', {className: 'placeholder', text: 'Connecting...'}));
    connectBtn.disabled = true;

    const resp = await fetch('/api/db-connection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        host: hostInput.value.trim(),
        port: portInput.value.trim() || 5432,
        database: databaseInput.value.trim(),
        user: userInput.value.trim(),
        password: passwordInput.value,
      }),
    });
    const data = await resp.json();
    connectBtn.disabled = false;
    clear(statusArea);

    if (data.error) {
      statusArea.appendChild(el('div', {className: 'error-box', text: data.error}));
      return;
    }

    await onConnected();
  });
}

// Builds one small inline icon from a *fixed, code-authored* SVG string -
// never with any variable/user-derived content interpolated in. innerHTML
// is otherwise avoided everywhere else in this page (see the top-of-script
// comment on why), but a hardcoded icon literal carries no injection
// surface at all, since nothing external ever reaches this function's
// argument. document.createElement can't be used for SVG directly - it
// requires the SVG namespace, which parsing markup already handles
// correctly.
function icon(svgMarkup) {
  const span = document.createElement('span');
  span.innerHTML = svgMarkup;
  return span.firstChild;
}

const _ICON_DATABASE = icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"></ellipse><path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5"></path><path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"></path></svg>').cloneNode(true);
const _ICON_FILE = icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path><path d="M12 18v-6"></path><path d="M9.5 14.5 12 12l2.5 2.5"></path></svg>').cloneNode(true);
const _ICON_SHEET = icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M3 9h18"></path><path d="M3 15h18"></path><path d="M9 3v18"></path></svg>').cloneNode(true);
const _ICON_CHECK = icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"></path></svg>').cloneNode(true);

// --- Connect Your Data (top-level, once-per-session) ---
// Lets a person connect a database, upload a file, or paste a Sheets link
// exactly once, instead of being asked again inside each of the 3 dataset
// cards below. `activeSourceKind` is intentionally only ever set by *this*
// section's own successful actions - a per-card "use a different source"
// override (see renderSourceChooser) still updates lastFileUpload/
// lastZipUpload for the *reuse* links, but must not silently change what
// every other still-unmapped card defaults to.
let activeSourceKind = null; // 'database' | 'file' | 'zip' | 'sheet' | null

async function loadConnectSection() {
  const section = document.getElementById('connect-section');
  if (activeSourceKind) {
    renderConnectedStatus(section);
    return;
  }
  const resp = await fetch('/api/tables');
  const data = await resp.json();
  if (data.connected) {
    // A database was already reachable (e.g. via SUPPLYMIND_PG_* env vars)
    // before this page ever loaded - treat it as this session's active
    // source too, same as one connected through the form below.
    activeSourceKind = 'database';
    renderConnectedStatus(section);
    return;
  }
  renderConnectChooser(section);
}

function connectSectionDescription() {
  if (activeSourceKind === 'database') return 'Connected to a database.';
  if (activeSourceKind === 'file' && lastFileUpload) return 'Using uploaded file "' + lastFileUpload.filename + '".';
  if (activeSourceKind === 'zip' && lastZipUpload) return 'Using uploaded archive "' + lastZipUpload.filename + '".';
  if (activeSourceKind === 'sheet' && lastSheetLink) return 'Using a connected Google Sheet.';
  return 'Connected.';
}

function renderConnectedStatus(section) {
  clear(section);
  section.appendChild(el('h2', {text: 'Connect Your Data'}));
  const card = el('div', {className: 'connect-status-card'});
  const left = el('div', {className: 'connect-status-left'});
  const iconWrap = el('div', {className: 'connect-status-icon'});
  iconWrap.appendChild(_ICON_CHECK.cloneNode(true));
  left.appendChild(iconWrap);
  const text = el('div');
  text.appendChild(el('div', {className: 'connect-status-title', text: 'Data source connected'}));
  text.appendChild(el('div', {className: 'connect-status-desc', text: connectSectionDescription()}));
  left.appendChild(text);
  card.appendChild(left);
  const changeBtn = el('button', {className: 'btn', text: 'Change'});
  changeBtn.addEventListener('click', function() {
    activeSourceKind = null;
    loadConnectSection();
  });
  card.appendChild(changeBtn);
  section.appendChild(card);
}

function buildConnectOption(iconNode, title, description, onClick) {
  const option = document.createElement('button');
  option.type = 'button';
  option.className = 'connect-option';
  const iconWrap = el('div', {className: 'connect-icon'});
  iconWrap.appendChild(iconNode);
  option.appendChild(iconWrap);
  option.appendChild(el('div', {className: 'connect-option-title', text: title}));
  option.appendChild(el('div', {className: 'connect-option-desc', text: description}));
  option.addEventListener('click', onClick);
  return option;
}

function renderConnectChooser(section) {
  clear(section);
  section.appendChild(el('h2', {text: 'Connect Your Data'}));
  const card = el('div', {className: 'connect-card'});
  card.appendChild(el('div', {
    className: 'connect-intro',
    text: 'Connect once here, and Customer Orders, Inventory, and Delivery Records below can all map from it - no need to enter this again for each one.',
  }));

  const options = el('div', {className: 'connect-options'});
  const formArea = el('div');

  options.appendChild(buildConnectOption(_ICON_DATABASE.cloneNode(true), 'Connect a Database', 'Postgres host, port, database, user, and password.', function() {
    clear(options);
    renderDbConnectionForm(formArea, function() { renderConnectChooser(section); }, function() {
      activeSourceKind = 'database';
      loadConnectSection();
      return Promise.resolve();
    });
  }));
  options.appendChild(buildConnectOption(_ICON_FILE.cloneNode(true), 'Upload a CSV or ZIP File', 'One file, or a zip of several, covering some or all of your data.', function() {
    clear(options);
    renderTopLevelUpload(formArea, section);
  }));
  options.appendChild(buildConnectOption(_ICON_SHEET.cloneNode(true), 'Connect a Google Sheet', 'A public "Publish to web as CSV" link.', function() {
    clear(options);
    renderTopLevelSheetConnect(formArea, section);
  }));

  card.appendChild(options);
  card.appendChild(formArea);
  section.appendChild(card);
}

function renderTopLevelUpload(container, section) {
  clear(container);
  container.appendChild(el('div', {
    className: 'placeholder',
    text: 'Upload a CSV file (or a .zip archive of several) - its columns will be offered when mapping each dataset below.',
  }));

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.csv,.zip,text/csv,application/zip';
  container.appendChild(fileInput);

  const uploadBtn = el('button', {className: 'btn btn-primary', text: 'Upload'});
  const backBtn = el('button', {className: 'btn', text: 'Back'});
  backBtn.addEventListener('click', function() { renderConnectChooser(section); });
  container.appendChild(uploadBtn);
  container.appendChild(backBtn);

  const statusArea = el('div');
  container.appendChild(statusArea);

  uploadBtn.addEventListener('click', async function() {
    const file = fileInput.files[0];
    clear(statusArea);
    if (!file) {
      statusArea.appendChild(el('div', {className: 'error-box', text: 'Please choose a file to upload.'}));
      return;
    }

    statusArea.appendChild(el('div', {className: 'placeholder', text: 'Uploading...'}));
    uploadBtn.disabled = true;

    const resp = await fetch('/api/uploads', {
      method: 'POST',
      headers: {'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)},
      body: file,
    });
    const data = await resp.json();
    uploadBtn.disabled = false;
    clear(statusArea);

    if (data.error) {
      statusArea.appendChild(el('div', {className: 'error-box', text: data.error}));
      return;
    }

    if (data.kind === 'zip') {
      lastZipUpload = {filename: data.filename, members: data.members};
      activeSourceKind = 'zip';
    } else {
      lastFileUpload = {fileId: data.file_id, filename: data.filename, columns: data.columns, suggestedMappings: data.suggested_mappings};
      activeSourceKind = 'file';
    }
    loadConnectSection();
  });
}

function renderTopLevelSheetConnect(container, section) {
  clear(container);
  container.appendChild(el('div', {
    className: 'placeholder',
    text: 'Paste a Google Sheets CSV link (File > Share > Publish to Web, pick the specific tab, choose CSV as the format). ' +
      'The sheet is read fresh every time it is checked or mapped, so later edits always show up.',
  }));

  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.className = 'search-input';
  urlInput.placeholder = 'https://docs.google.com/spreadsheets/d/.../pub?output=csv';
  container.appendChild(urlInput);

  const checkBtn = el('button', {className: 'btn btn-primary', text: 'Connect'});
  const backBtn = el('button', {className: 'btn', text: 'Back'});
  backBtn.addEventListener('click', function() { renderConnectChooser(section); });
  container.appendChild(checkBtn);
  container.appendChild(backBtn);

  const statusArea = el('div');
  container.appendChild(statusArea);

  checkBtn.addEventListener('click', async function() {
    const url = urlInput.value.trim();
    clear(statusArea);
    if (!url) {
      statusArea.appendChild(el('div', {className: 'error-box', text: 'Please paste a link before continuing.'}));
      return;
    }

    statusArea.appendChild(el('div', {className: 'placeholder', text: 'Checking link...'}));
    checkBtn.disabled = true;

    // Only checked for validity here, never cached - the sheet's own
    // content is always re-fetched fresh at mapping/preview time (see
    // sheet_mapping_service.py's own docstring for why), so nothing about
    // its columns is stored, only the URL itself.
    const resp = await fetch('/api/sheet-columns', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url}),
    });
    const data = await resp.json();
    checkBtn.disabled = false;
    clear(statusArea);

    if (data.error) {
      statusArea.appendChild(el('div', {className: 'error-box', text: data.error}));
      return;
    }

    lastSheetLink = {url: url};
    activeSourceKind = 'sheet';
    loadConnectSection();
  });
}

// The entry point once a database is connected: search-and-pick a table,
// with the same CSV/Sheets/custom-query alternatives still available as
// peers, since a connected database doesn't rule out mapping one
// particular dataset from a file instead.
function renderTablePicker(requirements, pickerArea, tables) {
  clear(pickerArea);

  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.className = 'search-input';
  searchInput.placeholder = 'Search your tables...';
  pickerArea.appendChild(searchInput);

  const resultsBox = el('div', {className: 'table-search-results'});
  pickerArea.appendChild(resultsBox);

  const cancelBtn = el('button', {className: 'btn', text: 'Cancel'});
  cancelBtn.addEventListener('click', function() { clear(pickerArea); });
  pickerArea.appendChild(cancelBtn);

  // Escape hatch for a dataset whose required columns are split across
  // more tables than this table picker (and the guided browse builder's
  // own single-join cap) can reach - write a SELECT that joins whatever
  // is needed, and map its result columns instead of a table's.
  const orQueryBtn = el('button', {className: 'btn-link', text: "Or write a custom SQL query"});
  orQueryBtn.addEventListener('click', function() { writeQueryForMapping(requirements, pickerArea); });
  pickerArea.appendChild(orQueryBtn);

  const orUploadBtn = el('button', {className: 'btn-link', text: 'Or upload a CSV file (or a .zip archive of several)'});
  orUploadBtn.addEventListener('click', function() { uploadFileForMapping(requirements, pickerArea); });
  pickerArea.appendChild(orUploadBtn);

  appendReuseUploadLinks(requirements, pickerArea);

  // Also no database needed - a public Google Sheets CSV link is fetched
  // fresh on every check/preview, so editing the sheet later shows up
  // here too, unlike an uploaded file's frozen snapshot.
  const orSheetBtn = el('button', {className: 'btn-link', text: 'Or connect a Google Sheets link'});
  orSheetBtn.addEventListener('click', function() { pasteSheetLinkForMapping(requirements, pickerArea); });
  pickerArea.appendChild(orSheetBtn);

  function renderResults(filterText) {
    clear(resultsBox);
    const matches = tables.filter(function(t) { return t.toLowerCase().indexOf(filterText.toLowerCase()) !== -1; });
    matches.forEach(function(name) {
      const btn = el('button', {className: 'table-item', text: name});
      btn.addEventListener('click', function() { pickTableForMapping(requirements, name, pickerArea); });
      resultsBox.appendChild(btn);
    });
    if (matches.length === 0) {
      resultsBox.appendChild(el('div', {className: 'placeholder', text: 'No tables match "' + filterText + '".'}));
    }
  }
  renderResults('');
  searchInput.addEventListener('input', function() { renderResults(searchInput.value); });
  searchInput.focus();
}

// Shared by both the table-picker path and the write-your-own-query path:
// one dropdown per required/optional field, pre-filled from `suggestions`
// but always overridable, blocking Save until every required field has a
// real column chosen. `onSave(columnMapping)` does the actual POST and
// returns the parsed JSON response; this function only handles the
// resulting error/not-connected/success states, identically either way.
function buildMappingFieldForm(requirements, columnNames, suggestions, formArea, onSave) {
  const fieldSelects = {};
  const unavailableChecks = {};
  const computeChecks = {}; // field.name -> {checkbox, baseSelect, offsetSelect}
  function dateColumnSelect() {
    const select = document.createElement('select');
    const blank = el('option', {text: 'Choose a column...'});
    blank.value = '';
    select.appendChild(blank);
    columnNames.forEach(function(name) {
      const opt = el('option', {text: name});
      opt.value = name;
      select.appendChild(opt);
    });
    return select;
  }
  function buildFieldRow(field, required) {
    const row = el('div', {className: 'mapping-form-row'});
    row.appendChild(el('label', {text: field.name + (required ? ' *' : '')}));
    const select = document.createElement('select');
    const blank = el('option', {text: required ? 'Choose a column...' : '-- none --'});
    blank.value = '';
    select.appendChild(blank);
    columnNames.forEach(function(name) {
      const opt = el('option', {text: name});
      opt.value = name;
      select.appendChild(opt);
    });
    const suggestion = suggestions[field.name];
    if (suggestion) select.value = suggestion;
    row.appendChild(select);
    fieldSelects[field.name] = select;

    if (!required) return row;

    // Required fields only: a dataset genuinely missing this column (e.g. a
    // transaction-level export with no safety_stock) shouldn't be blocked
    // from mapping everything else - checking this exempts just this field
    // from the "every required field must be mapped" rule below. Whatever
    // calculation needs it is skipped with a clear reason instead of
    // crashing (see StockoutRiskAgent's own handling of a missing field).
    const unavailableLabel = document.createElement('label');
    unavailableLabel.className = 'unavailable-check';
    const unavailableCheckbox = document.createElement('input');
    unavailableCheckbox.type = 'checkbox';
    unavailableChecks[field.name] = unavailableCheckbox;
    unavailableLabel.appendChild(unavailableCheckbox);
    unavailableLabel.appendChild(document.createTextNode('Not available in this dataset'));
    row.appendChild(unavailableLabel);

    // Date fields only: a real export that records a base date plus a
    // lead-time/transit day count (e.g. an order date + "days for shipment
    // (scheduled)") instead of the target date itself - compute it rather
    // than requiring a direct column that doesn't exist.
    let computeCheckbox = null;
    let computeRow = null;
    if (field.is_date) {
      const computeLabel = document.createElement('label');
      computeLabel.className = 'unavailable-check';
      computeCheckbox = document.createElement('input');
      computeCheckbox.type = 'checkbox';
      computeLabel.appendChild(computeCheckbox);
      computeLabel.appendChild(document.createTextNode('Compute from another date + a day offset'));
      row.appendChild(computeLabel);

      computeRow = el('div', {className: 'mapping-form-row mapping-compute-row'});
      computeRow.style.display = 'none';
      const baseSelect = dateColumnSelect();
      const offsetSelect = dateColumnSelect();
      computeRow.appendChild(el('label', {text: 'Base date column'}));
      computeRow.appendChild(baseSelect);
      computeRow.appendChild(el('label', {text: 'Day offset column'}));
      computeRow.appendChild(offsetSelect);
      computeChecks[field.name] = {checkbox: computeCheckbox, baseSelect: baseSelect, offsetSelect: offsetSelect, row: computeRow};

      computeCheckbox.addEventListener('change', function() {
        computeRow.style.display = computeCheckbox.checked ? 'flex' : 'none';
        select.disabled = computeCheckbox.checked || unavailableCheckbox.checked;
        if (computeCheckbox.checked) {
          select.value = '';
          unavailableCheckbox.checked = false;
        }
      });
    }

    unavailableCheckbox.addEventListener('change', function() {
      select.disabled = unavailableCheckbox.checked || (computeCheckbox && computeCheckbox.checked);
      if (unavailableCheckbox.checked) {
        select.value = '';
        if (computeCheckbox) {
          computeCheckbox.checked = false;
          computeRow.style.display = 'none';
        }
      }
    });

    row._computeRow = computeRow;
    return row;
  }
  requirements.required.forEach(function(f) {
    const row = buildFieldRow(f, true);
    formArea.appendChild(row);
    if (row._computeRow) formArea.appendChild(row._computeRow);
  });
  requirements.optional.forEach(function(f) { formArea.appendChild(buildFieldRow(f, false)); });

  const errorArea = el('div');
  formArea.appendChild(errorArea);

  const saveBtn = el('button', {className: 'btn btn-primary', text: 'Save Mapping'});
  saveBtn.addEventListener('click', async function() {
    clear(errorArea);
    function fieldIsSatisfied(f) {
      if (fieldSelects[f.name].value) return true;
      if (unavailableChecks[f.name].checked) return true;
      const compute = computeChecks[f.name];
      return !!(compute && compute.checkbox.checked && compute.baseSelect.value && compute.offsetSelect.value);
    }
    const missingRequired = requirements.required.filter(function(f) { return !fieldIsSatisfied(f); });
    if (missingRequired.length > 0) {
      errorArea.appendChild(el('div', {
        className: 'error-box',
        text: 'Please choose a column for (or mark "Not available", or finish "Compute from..." for): ' + missingRequired.map(function(f) { return f.name; }).join(', '),
      }));
      return;
    }
    const columnMapping = {};
    Object.keys(fieldSelects).forEach(function(fieldName) {
      const value = fieldSelects[fieldName].value;
      if (value) columnMapping[fieldName] = value;
    });
    const unavailableFields = Object.keys(unavailableChecks).filter(function(fieldName) {
      return unavailableChecks[fieldName].checked;
    });
    const computedDateFields = {};
    Object.keys(computeChecks).forEach(function(fieldName) {
      const compute = computeChecks[fieldName];
      if (compute.checkbox.checked && compute.baseSelect.value && compute.offsetSelect.value) {
        computedDateFields[fieldName] = {base_date_column: compute.baseSelect.value, offset_days_column: compute.offsetSelect.value};
      }
    });

    saveBtn.disabled = true;
    const result = await onSave(columnMapping, unavailableFields, computedDateFields);
    saveBtn.disabled = false;
    clear(errorArea);
    if (result.error) {
      errorArea.appendChild(el('div', {className: 'error-box', text: result.error}));
      return;
    }
    if (result.connected === false) {
      errorArea.appendChild(el('div', {className: 'not-connected', text: result.message}));
      return;
    }
    await loadMappingSection();
  });
  formArea.appendChild(saveBtn);
}

async function pickTableForMapping(requirements, tableName, pickerArea) {
  clear(pickerArea);
  pickerArea.appendChild(el('div', {className: 'placeholder', text: 'Loading "' + tableName + '"...'}));

  const data = await fetchColumns(tableName);
  clear(pickerArea);
  if (!data.connected) {
    pickerArea.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }
  if (!data.found) {
    pickerArea.appendChild(el('div', {className: 'not-connected', text: 'No columns found for "' + tableName + '".'}));
    return;
  }

  const suggestions = data.suggested_mappings[requirements.dataset_name] || {};
  const columnNames = data.columns.map(function(c) { return c.name; });

  pickerArea.appendChild(el('div', {className: 'placeholder', text: 'Mapping "' + tableName + '" to ' + requirements.label}));

  const cancelBtn = el('button', {className: 'btn', text: 'Cancel'});
  cancelBtn.addEventListener('click', function() { clear(pickerArea); });

  buildMappingFieldForm(requirements, columnNames, suggestions, pickerArea, async function(columnMapping, unavailableFields, computedDateFields) {
    const resp = await fetch('/api/mappings/' + encodeURIComponent(requirements.dataset_name), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({table: tableName, column_mapping: columnMapping, unavailable_fields: unavailableFields, computed_date_fields: computedDateFields}),
    });
    return resp.json();
  });
  pickerArea.appendChild(cancelBtn);
}

function writeQueryForMapping(requirements, pickerArea) {
  clear(pickerArea);

  pickerArea.appendChild(el('div', {
    className: 'placeholder',
    text: 'Write a SELECT statement for ' + requirements.label + '. Join any tables you need — the query will be validated and its result columns will be available to map below.',
  }));

  const textarea = document.createElement('textarea');
  textarea.className = 'sql-input';
  textarea.rows = 4;
  textarea.placeholder = 'SELECT ... FROM ... JOIN ... ON ...';
  pickerArea.appendChild(textarea);

  pickerArea.appendChild(el('div', {
    className: 'guardrail-note',
    text: 'Read-only queries only: a single SELECT (or WITH ... SELECT) statement. INSERT, UPDATE, DELETE, and other statements are not permitted.',
  }));

  const checkBtn = el('button', {className: 'btn btn-primary', text: 'Check Columns'});
  const backBtn = el('button', {className: 'btn', text: 'Back to Table Search'});
  backBtn.addEventListener('click', function() { startMapping(requirements); });
  pickerArea.appendChild(checkBtn);
  pickerArea.appendChild(backBtn);

  const statusArea = el('div');
  pickerArea.appendChild(statusArea);

  checkBtn.addEventListener('click', async function() {
    const queryText = textarea.value;
    clear(statusArea);
    statusArea.appendChild(el('div', {className: 'placeholder', text: 'Checking query...'}));
    checkBtn.disabled = true;

    const resp = await fetch('/api/query-columns', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: queryText}),
    });
    const data = await resp.json();
    checkBtn.disabled = false;
    clear(statusArea);

    if (data.error) {
      statusArea.appendChild(el('div', {className: 'error-box', text: data.error}));
      return;
    }
    if (!data.connected) {
      statusArea.appendChild(el('div', {className: 'not-connected', text: data.message}));
      return;
    }

    const suggestions = data.suggested_mappings[requirements.dataset_name] || {};
    statusArea.appendChild(el('div', {className: 'placeholder', text: "Map this query's columns to " + requirements.label}));
    const formArea = el('div');
    statusArea.appendChild(formArea);
    buildMappingFieldForm(requirements, data.columns, suggestions, formArea, async function(columnMapping, unavailableFields, computedDateFields) {
      const saveResp = await fetch('/api/mappings/' + encodeURIComponent(requirements.dataset_name) + '/from-query', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: queryText, column_mapping: columnMapping, unavailable_fields: unavailableFields, computed_date_fields: computedDateFields}),
      });
      return saveResp.json();
    });
  });
}

// Cached after any successful single-CSV upload, so mapping a second or
// third dataset out of the same file (e.g. one wide export that already
// covers every dataset this console needs) doesn't require re-uploading
// it - resets on page reload, which is fine for a same-session convenience.
let lastFileUpload = null; // {fileId, filename, columns, suggestedMappings}

// Cached after any successful zip upload, so mapping a second or third
// dataset from the same bundle doesn't require re-uploading it - resets
// on page reload, which is fine for a same-session convenience.
let lastZipUpload = null; // {filename, members}

// Cached after a link is connected via "Connect Your Data" - only the URL
// itself, never its columns/rows, since a Google Sheet is always read
// fresh at mapping/preview time (see sheet_mapping_service.py's own
// docstring for why) rather than treated as a frozen snapshot.
let lastSheetLink = null; // {url}

// Shared by a direct CSV upload and a member picked out of an uploaded zip -
// both end up with exactly the same shape (one file's columns to map into
// one dataset), so both render through this one form-building path.
function renderFileMappingForm(requirements, fileId, filename, columns, suggestedMappings, container) {
  clear(container);
  const suggestions = suggestedMappings[requirements.dataset_name] || {};
  container.appendChild(el('div', {className: 'placeholder', text: 'Map "' + filename + '" to ' + requirements.label}));
  const formArea = el('div');
  container.appendChild(formArea);
  buildMappingFieldForm(requirements, columns, suggestions, formArea, async function(columnMapping, unavailableFields, computedDateFields) {
    const saveResp = await fetch('/api/mappings/' + encodeURIComponent(requirements.dataset_name) + '/from-file', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_id: fileId, filename: filename, column_mapping: columnMapping, unavailable_fields: unavailableFields, computed_date_fields: computedDateFields}),
    });
    return saveResp.json();
  });
}

function renderZipMemberPicker(requirements, zipFilename, members, container) {
  clear(container);
  container.appendChild(el('div', {
    className: 'placeholder',
    text: 'Choose which file inside "' + zipFilename + '" contains the data for ' + requirements.label + '.',
  }));
  const listBox = el('div', {className: 'table-search-results'});
  members.forEach(function(member) {
    const btn = el('button', {className: 'table-item', text: member.filename + ' (' + member.columns.length + ' columns)'});
    btn.addEventListener('click', function() {
      renderFileMappingForm(requirements, member.file_id, member.filename, member.columns, member.suggested_mappings, container);
    });
    listBox.appendChild(btn);
  });
  container.appendChild(listBox);
}

function uploadFileForMapping(requirements, pickerArea) {
  clear(pickerArea);

  pickerArea.appendChild(el('div', {
    className: 'placeholder',
    text: 'Upload a CSV file (or a .zip archive containing several, one per dataset) for ' + requirements.label + '. Its header row will be used to map each required field below. No database connection is required.',
  }));

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.csv,.zip,text/csv,application/zip';
  pickerArea.appendChild(fileInput);

  const uploadBtn = el('button', {className: 'btn btn-primary', text: 'Upload'});
  const backBtn = el('button', {className: 'btn', text: 'Back to Table Search'});
  backBtn.addEventListener('click', function() { startMapping(requirements); });
  pickerArea.appendChild(uploadBtn);
  pickerArea.appendChild(backBtn);

  const statusArea = el('div');
  pickerArea.appendChild(statusArea);

  uploadBtn.addEventListener('click', async function() {
    const file = fileInput.files[0];
    clear(statusArea);
    if (!file) {
      statusArea.appendChild(el('div', {className: 'error-box', text: 'Please choose a file to upload.'}));
      return;
    }

    statusArea.appendChild(el('div', {className: 'placeholder', text: 'Uploading...'}));
    uploadBtn.disabled = true;

    // Pass the File straight through as the body instead of first copying it
    // into an ArrayBuffer in JS - fetch() streams a File/Blob body directly
    // from disk with a correct Content-Length, and materializing a large file
    // into an ArrayBuffer first is dramatically slower in Safari than in
    // other browsers (a real report: a ~90MB file appeared to hang
    // indefinitely on the old `await file.arrayBuffer()` step).
    const resp = await fetch('/api/uploads', {
      method: 'POST',
      headers: {'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)},
      body: file,
    });
    const data = await resp.json();
    uploadBtn.disabled = false;
    clear(statusArea);

    if (data.error) {
      statusArea.appendChild(el('div', {className: 'error-box', text: data.error}));
      return;
    }

    if (data.kind === 'zip') {
      lastZipUpload = {filename: data.filename, members: data.members};
      renderZipMemberPicker(requirements, data.filename, data.members, statusArea);
      return;
    }

    lastFileUpload = {fileId: data.file_id, filename: data.filename, columns: data.columns, suggestedMappings: data.suggested_mappings};
    renderFileMappingForm(requirements, data.file_id, data.filename, data.columns, data.suggested_mappings, statusArea);
  });
}

// Shared by the manual "paste a link" flow below and the "already
// connected a Sheet at the top" auto-flow: probes the URL fresh (never
// cached - see sheet_mapping_service.py's own docstring for why) and
// renders the mapping form directly into `container` on success.
async function probeAndRenderSheetForm(requirements, url, container) {
  clear(container);
  container.appendChild(el('div', {className: 'placeholder', text: 'Checking the connected Google Sheet...'}));

  const resp = await fetch('/api/sheet-columns', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: url}),
  });
  const data = await resp.json();
  clear(container);

  if (data.error) {
    container.appendChild(el('div', {className: 'error-box', text: data.error}));
    return;
  }

  const suggestions = data.suggested_mappings[requirements.dataset_name] || {};
  container.appendChild(el('div', {className: 'placeholder', text: 'Map this sheet to ' + requirements.label}));
  const formArea = el('div');
  container.appendChild(formArea);
  buildMappingFieldForm(requirements, data.columns, suggestions, formArea, async function(columnMapping, unavailableFields, computedDateFields) {
    const saveResp = await fetch('/api/mappings/' + encodeURIComponent(requirements.dataset_name) + '/from-sheet', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url, column_mapping: columnMapping, unavailable_fields: unavailableFields, computed_date_fields: computedDateFields}),
    });
    return saveResp.json();
  });
}

function pasteSheetLinkForMapping(requirements, pickerArea) {
  clear(pickerArea);

  pickerArea.appendChild(el('div', {
    className: 'placeholder',
    text: 'Paste a Google Sheets CSV link for ' + requirements.label +
      '. In Google Sheets, choose File > Share > Publish to Web, select the specific tab, and choose CSV as the format. ' +
      'No database connection is required, and the sheet is read fresh each time it is checked or previewed, so later edits will appear here automatically.',
  }));

  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.className = 'search-input';
  urlInput.placeholder = 'https://docs.google.com/spreadsheets/d/.../pub?output=csv';
  pickerArea.appendChild(urlInput);

  const checkBtn = el('button', {className: 'btn btn-primary', text: 'Check Link'});
  const backBtn = el('button', {className: 'btn', text: 'Back to Table Search'});
  backBtn.addEventListener('click', function() { startMapping(requirements); });
  pickerArea.appendChild(checkBtn);
  pickerArea.appendChild(backBtn);

  const statusArea = el('div');
  pickerArea.appendChild(statusArea);

  checkBtn.addEventListener('click', async function() {
    const url = urlInput.value.trim();
    if (!url) {
      clear(statusArea);
      statusArea.appendChild(el('div', {className: 'error-box', text: 'Please paste a link before continuing.'}));
      return;
    }
    checkBtn.disabled = true;
    await probeAndRenderSheetForm(requirements, url, statusArea);
    checkBtn.disabled = false;
  });
}

async function markUnavailable(datasetName) {
  await fetch('/api/mappings/' + encodeURIComponent(datasetName) + '/unavailable', {method: 'POST'});
  await loadMappingSection();
}

async function clearMapping(datasetName) {
  await fetch('/api/mappings/' + encodeURIComponent(datasetName), {method: 'DELETE'});
  await loadMappingSection();
}

async function previewMapping(datasetName) {
  const resultArea = document.getElementById('mapping-result-' + datasetName);
  clear(resultArea);
  resultArea.appendChild(el('div', {className: 'placeholder', text: 'Loading preview...'}));

  const resp = await fetch('/api/mappings/' + encodeURIComponent(datasetName) + '/preview');
  const data = await resp.json();
  clear(resultArea);

  if (data.error) {
    resultArea.appendChild(el('div', {className: 'error-box', text: data.error}));
    return;
  }
  if (!data.connected) {
    resultArea.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }

  resultArea.appendChild(buildRowsTable(data.rows));
  if (data.truncated) {
    resultArea.appendChild(el('div', {className: 'truncated-note', text: 'Showing the first ' + data.row_count + ' rows. There may be more.'}));
  }
}

loadConnectSection();
loadTables();
loadMappingSection();
</script>
</body>
</html>""")


def _parse_selection(payload: dict) -> QuerySelection:
    """Turns a client-submitted JSON payload into a QuerySelection, or raises ValueError.

    Only checks *shape* here (right keys, right types) - whether the named
    tables/columns actually exist is preview_runner.run_preview()'s job,
    checked against a live schema fetched fresh at that point, never
    trusted from what this payload claims.
    """
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")

    base_table = payload.get("base_table")
    if not isinstance(base_table, str) or not base_table:
        raise ValueError("'base_table' must be a non-empty string")

    raw_columns = payload.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("'columns' must be a non-empty list")
    columns = []
    for item in raw_columns:
        if not isinstance(item, dict) or not isinstance(item.get("table"), str) or not isinstance(item.get("column"), str):
            raise ValueError("each entry in 'columns' must be an object with 'table' and 'column' strings")
        columns.append(SelectedColumn(table=item["table"], column=item["column"]))

    join = None
    raw_join = payload.get("join")
    if raw_join is not None:
        if not isinstance(raw_join, dict):
            raise ValueError("'join' must be an object or null")
        for key in ("left_table", "left_column", "right_table", "right_column"):
            if not isinstance(raw_join.get(key), str) or not raw_join[key]:
                raise ValueError(f"'join.{key}' must be a non-empty string")
        join = JoinSpec(
            left_table=raw_join["left_table"],
            left_column=raw_join["left_column"],
            right_table=raw_join["right_table"],
            right_column=raw_join["right_column"],
        )

    return QuerySelection(base_table=base_table, columns=columns, join=join)


def _suggested_mappings_for(columns: list[str]) -> dict[str, dict[str, str | None]]:
    """A starting-point guess per known dataset for a set of real column names
    (from a table, a query, or an uploaded file), never applied on its own -
    every caller shows these as editable, pre-filled dropdowns a person still
    confirms before a mapping is ever saved."""
    return {requirements.dataset_name: suggest_mapping(columns, requirements) for requirements in ALL_DATASETS}


def _column_requirement_to_dict(c: ColumnRequirement) -> dict:
    return {"name": c.name, "description": c.description, "example": c.example, "is_date": c.is_date}


def _dataset_requirements_to_dict(requirements: DatasetRequirements) -> dict:
    return {
        "dataset_name": requirements.dataset_name,
        "label": requirements.label,
        "required": [_column_requirement_to_dict(c) for c in requirements.required],
        "optional": [_column_requirement_to_dict(c) for c in requirements.optional],
    }


def _mapping_status_dict(mapping: DatasetMapping | None) -> dict:
    if mapping is None:
        return {"status": "not_mapped"}
    if mapping.status == "unavailable":
        return {"status": "unavailable"}
    return {
        "status": "mapped",
        "source_kind": mapping.source_kind,
        "table": mapping.table,
        "query": mapping.query,
        "file_id": mapping.file_id,
        "filename": mapping.filename,
        "sheet_url": mapping.sheet_url,
        "column_mapping": mapping.column_mapping,
        "unavailable_fields": mapping.unavailable_fields,
        "computed_date_fields": mapping.computed_date_fields,
    }


def _parse_column_mapping(payload: dict) -> dict[str, str]:
    column_mapping = payload.get("column_mapping")
    if not isinstance(column_mapping, dict) or not column_mapping:
        raise ValueError("'column_mapping' must be a non-empty object")
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in column_mapping.items()):
        raise ValueError("'column_mapping' values must be non-empty column-name strings")
    return column_mapping


def _parse_unavailable_fields(payload: dict) -> frozenset[str]:
    """Optional: canonical field names the user has explicitly declared absent
    from this source, via the mapping form's per-field "Not available in this
    dataset" option. Defaults to none, so an older client that never sends
    this key behaves exactly as before."""
    unavailable_fields = payload.get("unavailable_fields", [])
    if not isinstance(unavailable_fields, list) or not all(isinstance(f, str) for f in unavailable_fields):
        raise ValueError("'unavailable_fields' must be a list of strings")
    return frozenset(unavailable_fields)


def _parse_computed_date_fields(payload: dict) -> dict[str, dict[str, str]]:
    """Optional: canonical date field names the user has declared computed rather
    than mapped directly, via the mapping form's "Compute from another date + a
    day offset" option - {canonical_field: {"base_date_column": ..., "offset_days_column": ...}}.
    Defaults to none, so an older client that never sends this key behaves exactly
    as before."""
    computed_date_fields = payload.get("computed_date_fields", {})
    if not isinstance(computed_date_fields, dict):
        raise ValueError("'computed_date_fields' must be an object")
    for field_name, spec in computed_date_fields.items():
        if (
            not isinstance(field_name, str)
            or not isinstance(spec, dict)
            or set(spec.keys()) != {"base_date_column", "offset_days_column"}
            or not all(isinstance(v, str) and v for v in spec.values())
        ):
            raise ValueError(
                "'computed_date_fields' values must be objects with non-empty "
                "'base_date_column' and 'offset_days_column' string fields"
            )
    return computed_date_fields


class DataConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # data_console's own JSON logger covers what's worth logging

    def do_GET(self) -> None:
        self._dispatch_safely(self._route_get)

    def do_POST(self) -> None:
        self._dispatch_safely(self._route_post)

    def do_DELETE(self) -> None:
        self._dispatch_safely(self._route_delete)

    def _dispatch_safely(self, route: Callable[[], None]) -> None:
        """Guarantees the client always gets *some* response, even when a route
        handler raises something no caller down that specific path was prepared
        to catch (a real example: a real-world CSV's non-UTF-8 encoding crashing
        deep inside file-reading code). Without this, BaseHTTPRequestHandler's
        default behavior is to log a traceback server-side and drop the
        connection with zero bytes sent - indistinguishable, from the browser's
        side, from an indefinite hang, since fetch() never resolves or rejects
        in a way the calling code was watching for."""
        try:
            route()
        except Exception as exc:
            logger.error(
                "data_console_unhandled_error",
                extra={"event": "data_console_unhandled_error", "outcome": "failure", "error_class": type(exc).__name__, "context": {"path": self.path}},
            )
            self._send_json(500, {"error": f"unexpected server error ({type(exc).__name__}): {exc}"})

    def _route_get(self) -> None:
        if self.path == "/":
            self._send_html(200, _PAGE_TEMPLATE.substitute(row_limit=str(PREVIEW_ROW_LIMIT)))
            return
        if self.path == "/api/tables":
            self._handle_list_tables()
            return
        if self.path == "/api/datasets":
            self._handle_list_datasets()
            return
        if self.path == "/api/mappings":
            self._handle_list_mappings()
            return
        if self.path == "/dashboard" or self.path == "/dashboard/":
            self._handle_dashboard_page("control_tower_real_data.html")
            return
        if self.path.startswith("/dashboard/"):
            self._handle_dashboard_page(self.path[len("/dashboard/"):])
            return
        match = _COLUMNS_PATH_RE.match(self.path)
        if match:
            self._handle_list_columns(unquote(match.group(1)))
            return
        match = _MAPPING_PREVIEW_PATH_RE.match(self.path)
        if match:
            self._handle_preview_mapping(unquote(match.group(1)))
            return
        self._send_json(404, {"error": "not found"})

    def _route_post(self) -> None:
        if self.path == "/api/preview":
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            try:
                payload = json.loads(raw_body or b"{}")
                selection = _parse_selection(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_preview(selection)
            return

        if self.path == "/api/query-columns":
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_query_columns(raw_body)
            return

        if self.path == "/api/db-connection":
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_connect_database(raw_body)
            return

        if self.path == "/api/uploads":
            try:
                raw_body = self._read_request_body(max_bytes=MAX_UPLOAD_BYTES)
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_upload(raw_body)
            return

        if self.path == "/api/sheet-columns":
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_sheet_columns(raw_body)
            return

        match = _MAPPING_UNAVAILABLE_PATH_RE.match(self.path)
        if match:
            self._handle_mark_unavailable(unquote(match.group(1)))
            return

        match = _MAPPING_FROM_QUERY_PATH_RE.match(self.path)
        if match:
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_save_mapping_from_query(unquote(match.group(1)), raw_body)
            return

        match = _MAPPING_FROM_FILE_PATH_RE.match(self.path)
        if match:
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_save_mapping_from_file(unquote(match.group(1)), raw_body)
            return

        match = _MAPPING_FROM_SHEET_PATH_RE.match(self.path)
        if match:
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_save_mapping_from_sheet(unquote(match.group(1)), raw_body)
            return

        match = _MAPPING_PATH_RE.match(self.path)
        if match:
            try:
                raw_body = self._read_request_body()
            except ValueError as exc:
                self._send_json(400, {"error": f"invalid request: {exc}"})
                return
            self._handle_save_mapping(unquote(match.group(1)), raw_body)
            return

        self._send_json(404, {"error": "not found"})

    def _route_delete(self) -> None:
        match = _MAPPING_PATH_RE.match(self.path)
        if match:
            self._handle_clear_mapping(unquote(match.group(1)))
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
        suggested_mappings = _suggested_mappings_for(column_names)
        self._send_json(
            200,
            {
                "connected": True,
                "found": True,
                "table": table_name,
                "columns": [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in columns],
                "suggested_mappings": suggested_mappings,
            },
        )

    def _handle_preview(self, selection: QuerySelection) -> None:
        try:
            result = run_preview(selection)
        except InvalidSelectionError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return

        self._send_json(
            200,
            {
                "connected": True,
                "sql": result.sql,
                "rows": result.rows,
                "row_count": result.row_count,
                "truncated": result.truncated,
            },
        )

    def _handle_list_datasets(self) -> None:
        self._send_json(200, {"datasets": [_dataset_requirements_to_dict(d) for d in ALL_DATASETS]})

    def _handle_list_mappings(self) -> None:
        store = MappingStore()
        mappings = {kind: _mapping_status_dict(store.get(kind)) for kind in BY_NAME}
        self._send_json(200, {"mappings": mappings})

    def _handle_dashboard_page(self, filename: str) -> None:
        """Serves one of the 3 pre-generated Executive Control Tower pages from
        dashboard/'s own output directory. filename must exactly match one this
        module's own allowlist names - see _DASHBOARD_FILENAMES's docstring on
        why this never touches the filesystem with an unvalidated request path.
        The dashboard pages' own nav bar already links to these exact filenames
        as relative hrefs (dashboard/render.py's _render_nav), so this needs no
        change on that side to keep working whether a page is opened directly
        from disk or through this route."""
        if filename not in _DASHBOARD_FILENAMES:
            self._send_json(404, {"error": f"unknown dashboard page {filename!r}"})
            return
        html_path = _DASHBOARD_HTML_DIR / filename
        if not html_path.is_file():
            self._send_html(
                404,
                "<!doctype html><html><head><meta charset=\"utf-8\"><title>Dashboard not generated yet</title></head>"
                "<body><h1>Dashboard not generated yet</h1><p>Run "
                "<code>python3 -m dashboard.run_sample_dashboard</code> from the repo root, then reload this "
                "page.</p></body></html>",
            )
            return
        self._send_html(200, html_path.read_text(encoding="utf-8"))

    def _handle_save_mapping(self, dataset_kind: str, raw_body: bytes) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            table = payload.get("table")
            if not isinstance(table, str) or not table:
                raise ValueError("'table' must be a non-empty string")
            column_mapping = _parse_column_mapping(payload)
            unavailable_fields = _parse_unavailable_fields(payload)
            computed_date_fields = _parse_computed_date_fields(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            save_mapping(dataset_kind, table, column_mapping, unavailable_fields, computed_date_fields)
        except SchemaMappingError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return

        self._send_json(200, {"saved": True})

    def _handle_query_columns(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body or b"{}")
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("'query' must be a non-empty string")
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            columns = probe_query_columns(query)
        except UnsafeQueryError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            # Covers a genuine SQL error in the typed query (unknown
            # column/table, syntax mistake) as well as an unreachable
            # database - postgres_connector doesn't distinguish the two,
            # and either way there is nothing more specific to say than
            # "here's what the database reported."
            self._send_json(400, {"error": f"Could not run that query: {exc}"})
            return

        self._send_json(200, {"connected": True, "columns": columns, "suggested_mappings": _suggested_mappings_for(columns)})

    def _handle_save_mapping_from_query(self, dataset_kind: str, raw_body: bytes) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("'query' must be a non-empty string")
            column_mapping = _parse_column_mapping(payload)
            unavailable_fields = _parse_unavailable_fields(payload)
            computed_date_fields = _parse_computed_date_fields(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            save_mapping_from_query(dataset_kind, query, column_mapping, unavailable_fields, computed_date_fields)
        except UnsafeQueryError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SchemaMappingError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return

        self._send_json(200, {"saved": True})

    def _handle_connect_database(self, raw_body: bytes) -> None:
        """Accepts connection details typed into "Connect a Database", tests them for
        real before committing anything, and - only on success - sets them as this
        session's runtime override so every other Postgres-backed route (table
        browsing, custom queries, table mapping) picks them up immediately. The
        password is never echoed back, logged, or written to disk anywhere on this
        path - see runtime_db_config.py's own docstring for why."""
        try:
            payload = json.loads(raw_body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            host = payload.get("host")
            database = payload.get("database")
            user = payload.get("user")
            password = payload.get("password")
            if not isinstance(host, str) or not host.strip():
                raise ValueError("'host' must be a non-empty string")
            if not isinstance(database, str) or not database.strip():
                raise ValueError("'database' must be a non-empty string")
            if not isinstance(user, str) or not user.strip():
                raise ValueError("'user' must be a non-empty string")
            if not isinstance(password, str) or not password:
                raise ValueError("'password' must be a non-empty string")
            port_raw = payload.get("port", 5432)
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                raise ValueError("'port' must be a valid integer") from None
            config = PostgresConfig(host=host.strip(), port=port, database=database.strip(), user=user.strip(), password=password)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            fetch_rows("SELECT 1", config=config)
        except PostgresIntegrationError as exc:
            self._send_json(400, {"error": f"Could not connect: {exc}"})
            return

        set_runtime_config(config)
        self._send_json(200, {"connected": True, "host": config.host, "port": config.port, "database": config.database, "user": config.user})

    def _handle_upload(self, raw_body: bytes) -> None:
        # URL-decoded since a raw HTTP header value can't safely carry an
        # arbitrary filename (non-ASCII characters, in particular) -
        # the browser side encodes it with encodeURIComponent() first.
        filename = unquote(self.headers.get("X-Filename", "")).strip()
        if not filename:
            self._send_json(400, {"error": "invalid request: 'X-Filename' header must be set to a non-empty filename"})
            return
        if not raw_body:
            self._send_json(400, {"error": "invalid request: uploaded file is empty"})
            return

        if filename.lower().endswith(".zip"):
            try:
                extracted = extract_csvs_from_zip(raw_body)
            except UnsafeArchiveError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            members = [self._probed_upload(file_id, member_filename) for file_id, member_filename in extracted]
            self._send_json(200, {"kind": "zip", "filename": filename, "members": members})
            return

        file_id = save_upload(filename, raw_body)
        self._send_json(200, {"kind": "csv", **self._probed_upload(file_id, filename)})

    def _probed_upload(self, file_id: str, filename: str) -> dict:
        columns = probe_upload_columns(file_id)
        return {"file_id": file_id, "filename": filename, "columns": columns, "suggested_mappings": _suggested_mappings_for(columns)}

    def _handle_save_mapping_from_file(self, dataset_kind: str, raw_body: bytes) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            file_id = payload.get("file_id")
            filename = payload.get("filename")
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("'file_id' must be a non-empty string")
            if not isinstance(filename, str) or not filename:
                raise ValueError("'filename' must be a non-empty string")
            column_mapping = _parse_column_mapping(payload)
            unavailable_fields = _parse_unavailable_fields(payload)
            computed_date_fields = _parse_computed_date_fields(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            save_mapping_from_file(dataset_kind, file_id, filename, column_mapping, unavailable_fields, computed_date_fields)
        except UnknownUploadError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SchemaMappingError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send_json(200, {"saved": True})

    def _handle_sheet_columns(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body or b"{}")
            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("'url' must be a non-empty string")
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            columns = probe_sheet_columns(url)
        except (InvalidSheetUrlError, SheetFetchError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send_json(200, {"connected": True, "columns": columns, "suggested_mappings": _suggested_mappings_for(columns)})

    def _handle_save_mapping_from_sheet(self, dataset_kind: str, raw_body: bytes) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("'url' must be a non-empty string")
            column_mapping = _parse_column_mapping(payload)
            unavailable_fields = _parse_unavailable_fields(payload)
            computed_date_fields = _parse_computed_date_fields(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            save_mapping_from_sheet(dataset_kind, url, column_mapping, unavailable_fields, computed_date_fields)
        except (InvalidSheetUrlError, SheetFetchError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SchemaMappingError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send_json(200, {"saved": True})

    def _handle_mark_unavailable(self, dataset_kind: str) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return
        mark_unavailable(dataset_kind)
        self._send_json(200, {"saved": True})

    def _handle_clear_mapping(self, dataset_kind: str) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return
        clear_mapping(dataset_kind)
        self._send_json(200, {"cleared": True})

    def _handle_preview_mapping(self, dataset_kind: str) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            result = preview_mapping(dataset_kind)
        except LookupError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SchemaMappingError as exc:
            # The live schema no longer matches this saved mapping (a
            # column was renamed/dropped since Save Mapping was clicked) -
            # a clear, actionable error, distinct from "not connected"
            # since the database itself is reachable fine.
            self._send_json(400, {"error": str(exc)})
            return
        except (InvalidSheetUrlError, SheetFetchError) as exc:
            # A sheet-backed mapping whose link stopped working since Save
            # Mapping (sharing revoked, sheet deleted) - same "clear,
            # actionable error" treatment as a schema change above, not a
            # "not connected" banner, since there is no database here at all.
            self._send_json(400, {"error": str(exc)})
            return
        except MissingConfigError as exc:
            self._send_not_connected(str(exc))
            return
        except PostgresIntegrationError as exc:
            self._send_not_connected(f"Could not reach the database: {exc}")
            return

        self._send_json(
            200, {"connected": True, "rows": result.rows, "row_count": result.row_count, "truncated": result.truncated}
        )

    def _send_not_connected(self, message: str) -> None:
        logger.info(
            "data_console_not_connected",
            extra={"event": "data_console_not_connected", "outcome": "failure", "context": {"message": message}},
        )
        self._send_json(200, {"connected": False, "message": message})

    def _read_request_body(self, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> bytes:
        """Same guarded read chat_interface/serve_chat_ui.py's _read_request_body() uses, for the
        same reason: a non-numeric Content-Length would otherwise crash with no response at all,
        and a negative one would block self.rfile.read() forever, wedging this single-threaded
        server for every other client. `max_bytes` defaults to the small JSON-API cap; the file
        upload route passes the much larger MAX_UPLOAD_BYTES instead."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError(f"Content-Length {raw_length!r} is not a valid integer") from exc
        if length < 0:
            raise ValueError(f"Content-Length {length} must not be negative")
        if length > max_bytes:
            raise ValueError(f"Content-Length {length} exceeds the {max_bytes}-byte limit")
        return self.rfile.read(length) if length else b""

    def _send_html(self, status: int, body_str: str) -> None:
        body = body_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
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
