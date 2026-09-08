"""Durable local storage for this console's dataset-to-table-and-column mappings.

A tiny JSON file, not a database - there are only ever 3 records (one per
known dataset kind), read and rewritten as a whole each time, which is
simpler and plenty fast for that size. Not committed to the repo (see
.gitignore): a mapping encodes real schema details (table and column
names) about whatever database this console happens to be pointed at,
which has no business riding along in a public portfolio repo the way
docs/screenshots or code does.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

DatasetMappingStatus = Literal["mapped", "unavailable"]

DEFAULT_MAPPINGS_PATH = Path(__file__).resolve().parent / ".mappings.json"


DatasetMappingSource = Literal["table", "query"]


@dataclass(frozen=True)
class DatasetMapping:
    status: DatasetMappingStatus
    table: str | None = None
    column_mapping: dict[str, str] = field(default_factory=dict)
    # "table" (the guided picker - table points straight at a real table) or
    # "query" (the raw-SQL fallback - table is None, query holds the SQL
    # text instead). Defaults to "table" so a mapping saved before this
    # field existed still loads correctly.
    source_kind: DatasetMappingSource = "table"
    query: str | None = None


class MappingStore:
    """Not safe for concurrent multi-process writers (no file locking) - same caveat
    every *AuditStore in this repo documents for itself; this console runs as a
    single local process."""

    def __init__(self, path: str | Path = DEFAULT_MAPPINGS_PATH):
        self._path = Path(path)

    def load_all(self) -> dict[str, DatasetMapping]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupted or unreadable mappings file must not crash the
            # console - it just means every dataset shows as "not mapped
            # yet" again, the same safe starting state as a brand new file.
            return {}

        result: dict[str, DatasetMapping] = {}
        for kind, data in raw.items():
            if not isinstance(data, dict):
                continue
            try:
                result[kind] = DatasetMapping(**data)
            except TypeError:
                continue
        return result

    def get(self, dataset_kind: str) -> DatasetMapping | None:
        return self.load_all().get(dataset_kind)

    def save(self, dataset_kind: str, mapping: DatasetMapping) -> None:
        all_mappings = self.load_all()
        all_mappings[dataset_kind] = mapping
        self._write(all_mappings)

    def clear(self, dataset_kind: str) -> None:
        all_mappings = self.load_all()
        all_mappings.pop(dataset_kind, None)
        self._write(all_mappings)

    def _write(self, all_mappings: dict[str, DatasetMapping]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {kind: asdict(mapping) for kind, mapping in all_mappings.items()}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
