"""Audit trail for scenario simulations (STORY-008 / REQ-014).

Every simulation attempt gets one persisted record: which simulation
run, which scenario and SKU it evaluated, the input parameters that
defined the what-if (demand/lead-time/safety-stock/stock deltas), the
resulting impact, and a timestamp - this is what satisfies AC3 ("Trust:
the system logs all scenario simulations with timestamps and input
parameters"). Records are keyed by an idempotency key -
`f"{simulation_id}:{scenario_name}:{sku}"` - so re-recording the same
scenario/SKU within the same run does not create a duplicate audit
entry; the existing record is returned instead of a new one being
written.

This mirrors root_cause/audit_trail.py's RootCauseAuditStore (STORY-007)
almost exactly, just re-keyed for "one scenario against one SKU" instead
of "one issue of one analysis run", with input_parameters added to the
persisted record since AC3 explicitly requires those (root cause's own
AC3 only requires confidence levels, not inputs) - same JSONL
persistence, same corrupted-line tolerance, same idempotent record()
semantics, so every trust-spine implementation in this repo behaves
identically to anyone auditing any of them.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from scenario_simulation.logging_setup import get_logger

logger = get_logger()

SimulationOutcome = Literal["success", "failure"]


def _idempotency_key(simulation_id: str, scenario_name: str, sku: str) -> str:
    return f"{simulation_id}:{scenario_name}:{sku}"


def _json_safe(value: float | None) -> float | None:
    # json.dumps emits the non-standard Infinity/NaN tokens for those
    # floats, which isn't valid JSON for downstream consumers that parse
    # this JSONL strictly - normalize to None at the audit boundary, same
    # convention agents/stockout_risk_agent.py already applies at its own
    # logging boundary.
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return value


@dataclass(frozen=True)
class ScenarioSimulationAuditRecord:
    record_id: str
    idempotency_key: str
    simulation_id: str
    scenario_name: str
    sku: str
    outcome: SimulationOutcome
    timestamp: str
    input_parameters: dict[str, Any] = field(default_factory=dict)
    risk_level_changed: bool | None = None
    days_of_supply_delta: float | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "simulation_id": self.simulation_id,
            "scenario_name": self.scenario_name,
            "sku": self.sku,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "input_parameters": self.input_parameters,
            "risk_level_changed": self.risk_level_changed,
            "days_of_supply_delta": _json_safe(self.days_of_supply_delta),
            "detail": self.detail,
        }


class ScenarioSimulationAuditWriteError(RuntimeError):
    """Raised when a scenario simulation audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail not recorded for simulations" failure
    mode: if the trail can't be written, the caller must know about it,
    not proceed as if the record landed.
    """


class ScenarioSimulationAuditStore:
    """JSONL-backed audit trail of scenario simulations, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) -
    scenario simulation runs as a single process today, same caveat as
    every other audit store in this repo (root_cause/audit_trail.py,
    supplier_evaluation/audit_trail.py, intelligence/audit_trail.py,
    data_integration/audit_trail.py).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, ScenarioSimulationAuditRecord] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """Load prior records, tolerating individual corrupted lines.

        JSONL appends aren't atomic - a process killed mid-write can leave
        a truncated trailing line. That single bad line must not brick the
        whole audit trail on the next startup, so it's logged and skipped
        rather than raised. Only a failure to open/read the file at all is
        fatal.
        """
        if not self._path.exists() or not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            raise ScenarioSimulationAuditWriteError(
                f"could not read existing scenario simulation audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = ScenarioSimulationAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "scenario_simulation_audit_record_skipped_corrupted",
                    extra={
                        "event": "scenario_simulation_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, simulation_id: str, scenario_name: str, sku: str) -> bool:
        return _idempotency_key(simulation_id, scenario_name, sku) in self._records

    def records_for_simulation(self, simulation_id: str) -> list[ScenarioSimulationAuditRecord]:
        return [r for r in self._records.values() if r.simulation_id == simulation_id]

    def record(
        self,
        *,
        simulation_id: str,
        scenario_name: str,
        sku: str,
        outcome: SimulationOutcome,
        input_parameters: dict[str, Any] | None = None,
        risk_level_changed: bool | None = None,
        days_of_supply_delta: float | None = None,
        detail: str | None = None,
    ) -> ScenarioSimulationAuditRecord:
        """Persist one scenario-simulation record, unless (simulation_id, scenario_name, sku) was already seen.

        Returns the new record, or the existing one if this
        (simulation_id, scenario_name, sku) triple was already recorded -
        re-recording the same scenario/SKU within the same run must not
        duplicate the trail.
        """
        idempotency_key = _idempotency_key(simulation_id, scenario_name, sku)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "scenario_simulation_audit_duplicate_skipped",
                    extra={
                        "event": "scenario_simulation_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": simulation_id,
                        "context": {"scenario_name": scenario_name, "sku": sku, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = ScenarioSimulationAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                simulation_id=simulation_id,
                scenario_name=scenario_name,
                sku=sku,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                input_parameters=input_parameters or {},
                risk_level_changed=risk_level_changed,
                days_of_supply_delta=days_of_supply_delta,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "scenario_simulation_audit_trail_write_failed",
                    extra={
                        "event": "scenario_simulation_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": simulation_id,
                        "context": {"scenario_name": scenario_name, "sku": sku, "idempotency_key": idempotency_key},
                    },
                )
                raise ScenarioSimulationAuditWriteError(
                    f"failed to write scenario simulation audit record for simulation "
                    f"{simulation_id!r} scenario {scenario_name!r} sku {sku!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
