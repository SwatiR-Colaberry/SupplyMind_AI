"""Local browser UI for connecting to, mapping, and browsing a database (data-console).

Covers Slices 1-3 of the agreed build order (same "one step at a time"
rhythm used for chat_interface/serve_chat_ui.py): Slice 1 connects to
Postgres and browses tables/columns against this repo's three known
required datasets; Slice 2 adds picking specific columns, joining a
second table, and a capped preview; Slice 3 adds "Map Your Data" - for
each of the 3 known datasets, point it at whichever table actually holds
that data even when its column names differ, using
data_integration/connection_profile.py's real mapping/validation engine.
No CSV/Sheets upload, no chat yet - those remain later slices.

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
from string import Template
from urllib.parse import unquote

from data_console import schema_inspector
from data_console.column_requirements import ALL_DATASETS, BY_NAME, DatasetRequirements
from data_console.logging_setup import get_logger
from data_console.mapping_service import clear_mapping, mark_unavailable, preview_mapping, save_mapping
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.mapping_suggester import suggest_mapping
from data_console.preview_runner import run_preview
from data_console.query_builder import PREVIEW_ROW_LIMIT, InvalidSelectionError, JoinSpec, QuerySelection, SelectedColumn
from data_integration.config import MissingConfigError
from data_integration.connection_profile import SchemaMappingError
from data_integration.postgres_connector import PostgresIntegrationError

logger = get_logger()

DEFAULT_PORT = 8766

# A selection with a handful of columns and one join is nowhere near this
# size; the cap exists purely so a malformed/absurd Content-Length can
# never make this single-threaded dev server misbehave, same reasoning
# and same value as chat_interface/serve_chat_ui.py's own constant.
MAX_REQUEST_BODY_BYTES = 65536

_COLUMNS_PATH_RE = re.compile(r"^/api/tables/([^/]+)/columns$")
_MAPPING_PATH_RE = re.compile(r"^/api/mappings/([^/]+)$")
_MAPPING_UNAVAILABLE_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/unavailable$")
_MAPPING_PREVIEW_PATH_RE = re.compile(r"^/api/mappings/([^/]+)/preview$")


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
  .select-section { margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; }
  .col-checkbox-row { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 0; }
  .btn { padding: 6px 12px; border: 1px solid #1565c0; background: white; color: #1565c0; border-radius: 6px; font-size: 12px; cursor: pointer; margin: 4px 4px 4px 0; }
  .btn:hover { background: #eaf1fb; }
  .btn-primary { background: #1565c0; color: white; }
  .btn-primary:hover { background: #114f96; }
  .join-block { margin: 10px 0; padding: 10px 12px; background: #f8f9fb; border-radius: 6px; }
  .join-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 8px; flex-wrap: wrap; }
  select { padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 12px; }
  .sql-box { background: #1a1a2e; color: #d6e4ff; padding: 10px 12px; border-radius: 6px; font-size: 11px; overflow-x: auto; margin: 10px 0; white-space: pre-wrap; word-break: break-word; }
  .truncated-note { font-size: 11px; color: #888; margin-top: 4px; }
  .error-box { background: #fdecea; border: 1px solid #f5c2c0; color: #8a1f11; padding: 8px 12px; border-radius: 6px; font-size: 12px; margin: 10px 0; }
  .mapping-section { margin-bottom: 24px; }
  .mapping-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
  .mapping-card { background: white; border-radius: 8px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
  .mapping-card.status-mapped { border-left: 4px solid #2e7d32; }
  .mapping-card.status-not_mapped { border-left: 4px solid #f9a825; }
  .mapping-card.status-unavailable { border-left: 4px solid #757575; }
  .mapping-card h3 { font-size: 14px; margin: 0 0 4px; }
  .mapping-status-line { font-size: 12px; color: #444; margin-bottom: 8px; }
  .mapping-fields { font-size: 12px; color: #555; margin-bottom: 10px; }
  .mapping-fields .field-line { margin: 2px 0; }
  .mapping-fields .field-optional { color: #888; }
  .search-input { width: 100%; padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 12px; margin-bottom: 8px; box-sizing: border-box; }
  .table-search-results { max-height: 160px; overflow-y: auto; border: 1px solid #eee; border-radius: 4px; margin-bottom: 8px; }
  .mapping-form-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 6px 0; }
  .mapping-form-row label { width: 150px; flex-shrink: 0; }
  .mapping-form-row select { flex: 1; }
  .table-scroll { overflow-x: auto; max-width: 100%; }
</style>
</head>
<body>
  <h1>Connect Your Data</h1>
  <div class="meta">data console - browse what's in your database before selecting anything to analyze</div>
  <div class="intro">
    This page reads what's already in your database and shows it to you plainly - it doesn't change or
    move anything. Start below by pointing each of the 3 things this system needs (Customer Orders,
    Inventory, Delivery Records) at whichever table in your database actually holds that data, even if
    its column names are different from ours - you'll see exactly what's required before you have to
    pick anything. Below that, you can also just browse any table freely, pick columns, join a second
    table, and preview the result - every preview is capped at $row_limit rows so this page never tries
    to load your whole database at once.
  </div>
  <div class="mapping-section" id="mapping-section">
    <h2>Map Your Data</h2>
    <div class="placeholder">Loading...</div>
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
    content.appendChild(el('div', {className: 'not-connected', text: data.message}));
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
  detailPanel.appendChild(el('div', {className: 'placeholder', text: 'Click a table to see its columns.'}));
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

  section.appendChild(el('h2', {text: 'Build a selection'}));
  section.appendChild(el('div', {className: 'placeholder', text: 'Choose which columns to include from "' + tableName + '" (all checked by default). Join in a second table if what you need is split across two.'}));
  section.appendChild(buildColumnCheckboxes(tableName, columns, true));

  const joinArea = el('div');
  joinArea.id = 'join-area';
  const addJoinBtn = el('button', {className: 'btn', text: '+ Join another table'});
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
    joinArea.appendChild(el('div', {className: 'placeholder', text: 'No other tables available to join.'}));
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
  joinRow.appendChild(document.createTextNode('Match column in ' + baseTable + ':'));
  const leftSelect = document.createElement('select');
  leftSelect.id = 'join-left-column';
  baseColumns.forEach(function(c) {
    const opt = el('option', {text: c.name});
    opt.value = c.name;
    leftSelect.appendChild(opt);
  });
  joinRow.appendChild(leftSelect);

  joinRow.appendChild(document.createTextNode('to column in ' + joinTableName + ':'));
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
  block.appendChild(el('div', {className: 'placeholder', text: 'Columns from "' + joinTableName + '" to include:'}));
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
    resultArea.appendChild(el('div', {className: 'truncated-note', text: 'Showing the first ' + data.row_count + ' rows - there may be more.'}));
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
    line.appendChild(document.createTextNode(f.name + ' (required) - ' + f.description + ' e.g. "' + f.example + '"'));
    container.appendChild(line);
  });
  requirements.optional.forEach(function(f) {
    const line = el('div', {className: 'field-line field-optional'});
    line.appendChild(document.createTextNode(f.name + ' (optional) - ' + f.description + ' e.g. "' + f.example + '"'));
    container.appendChild(line);
  });
  return container;
}

function mappingStatusLine(status) {
  if (status.status === 'mapped') return 'Mapped to "' + status.table + '"';
  if (status.status === 'unavailable') return 'Marked as not available in this database';
  return 'Not mapped yet';
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
    card.appendChild(el('h3', {text: requirements.label}));
    card.appendChild(el('div', {className: 'mapping-status-line', text: mappingStatusLine(status)}));
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
      const mapBtn = el('button', {className: 'btn btn-primary', text: 'Map this'});
      mapBtn.addEventListener('click', function() { startMapping(requirements); });
      actions.appendChild(mapBtn);
      if (status.status === 'unavailable') {
        const clearBtn = el('button', {className: 'btn', text: 'Undo "not available"'});
        clearBtn.addEventListener('click', function() { clearMapping(requirements.dataset_name); });
        actions.appendChild(clearBtn);
      } else {
        const unavailableBtn = el('button', {className: 'btn', text: 'Not available in this database'});
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

  const resp = await fetch('/api/tables');
  const data = await resp.json();
  if (!data.connected) {
    clear(pickerArea);
    pickerArea.appendChild(el('div', {className: 'not-connected', text: data.message}));
    return;
  }

  function renderResults(filterText) {
    clear(resultsBox);
    const matches = data.tables.filter(function(t) { return t.toLowerCase().indexOf(filterText.toLowerCase()) !== -1; });
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

  pickerArea.appendChild(el('div', {className: 'placeholder', text: 'Mapping "' + tableName + '" to ' + requirements.label + ':'}));

  const fieldSelects = {};
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
    return row;
  }
  requirements.required.forEach(function(f) { pickerArea.appendChild(buildFieldRow(f, true)); });
  requirements.optional.forEach(function(f) { pickerArea.appendChild(buildFieldRow(f, false)); });

  const errorArea = el('div');
  pickerArea.appendChild(errorArea);

  const saveBtn = el('button', {className: 'btn btn-primary', text: 'Save Mapping'});
  const cancelBtn = el('button', {className: 'btn', text: 'Cancel'});
  cancelBtn.addEventListener('click', function() { clear(pickerArea); });
  saveBtn.addEventListener('click', async function() {
    clear(errorArea);
    const missingRequired = requirements.required.filter(function(f) { return !fieldSelects[f.name].value; });
    if (missingRequired.length > 0) {
      errorArea.appendChild(el('div', {
        className: 'error-box',
        text: 'Choose a column for: ' + missingRequired.map(function(f) { return f.name; }).join(', '),
      }));
      return;
    }
    const columnMapping = {};
    Object.keys(fieldSelects).forEach(function(fieldName) {
      const value = fieldSelects[fieldName].value;
      if (value) columnMapping[fieldName] = value;
    });

    saveBtn.disabled = true;
    const resp = await fetch('/api/mappings/' + encodeURIComponent(requirements.dataset_name), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({table: tableName, column_mapping: columnMapping}),
    });
    const result = await resp.json();
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
  pickerArea.appendChild(saveBtn);
  pickerArea.appendChild(cancelBtn);
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
    resultArea.appendChild(el('div', {className: 'truncated-note', text: 'Showing the first ' + data.row_count + ' rows - there may be more.'}));
  }
}

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


def _dataset_requirements_to_dict(requirements: DatasetRequirements) -> dict:
    return {
        "dataset_name": requirements.dataset_name,
        "label": requirements.label,
        "required": [{"name": c.name, "description": c.description, "example": c.example} for c in requirements.required],
        "optional": [{"name": c.name, "description": c.description, "example": c.example} for c in requirements.optional],
    }


def _mapping_status_dict(mapping: DatasetMapping | None) -> dict:
    if mapping is None:
        return {"status": "not_mapped"}
    if mapping.status == "unavailable":
        return {"status": "unavailable"}
    return {"status": "mapped", "table": mapping.table, "column_mapping": mapping.column_mapping}


class DataConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # data_console's own JSON logger covers what's worth logging

    def do_GET(self) -> None:
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
        match = _COLUMNS_PATH_RE.match(self.path)
        if match:
            self._handle_list_columns(unquote(match.group(1)))
            return
        match = _MAPPING_PREVIEW_PATH_RE.match(self.path)
        if match:
            self._handle_preview_mapping(unquote(match.group(1)))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
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

        match = _MAPPING_UNAVAILABLE_PATH_RE.match(self.path)
        if match:
            self._handle_mark_unavailable(unquote(match.group(1)))
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

    def do_DELETE(self) -> None:
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
        # A starting-point guess per dataset (e.g. "sku" for a table whose
        # real column is "item_sku"), never applied on its own - the
        # mapping UI shows these as editable, pre-filled dropdowns a
        # person still confirms before Save Mapping is ever called.
        suggested_mappings = {
            requirements.dataset_name: suggest_mapping(column_names, requirements) for requirements in ALL_DATASETS
        }
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

    def _handle_save_mapping(self, dataset_kind: str, raw_body: bytes) -> None:
        if dataset_kind not in BY_NAME:
            self._send_json(404, {"error": f"unknown dataset {dataset_kind!r}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            table = payload.get("table")
            column_mapping = payload.get("column_mapping")
            if not isinstance(table, str) or not table:
                raise ValueError("'table' must be a non-empty string")
            if not isinstance(column_mapping, dict) or not column_mapping:
                raise ValueError("'column_mapping' must be a non-empty object")
            if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in column_mapping.items()):
                raise ValueError("'column_mapping' values must be non-empty column-name strings")
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            save_mapping(dataset_kind, table, column_mapping)
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

    def _read_request_body(self) -> bytes:
        """Same guarded read chat_interface/serve_chat_ui.py's _read_request_body() uses, for the
        same reason: a non-numeric Content-Length would otherwise crash with no response at all,
        and a negative one would block self.rfile.read() forever, wedging this single-threaded
        server for every other client."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError(f"Content-Length {raw_length!r} is not a valid integer") from exc
        if length < 0:
            raise ValueError(f"Content-Length {length} must not be negative")
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError(f"Content-Length {length} exceeds the {MAX_REQUEST_BODY_BYTES}-byte limit")
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
