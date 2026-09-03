"""Orchestrates shipment delay analysis with a durable audit trail (STORY-014 / REQ-008).

Ties analyze_shipment_delays() (delay_analysis.py, pure computation) to
ShipmentDelayAuditStore (audit_trail.py, persistence) the same way
supplier_evaluation/evaluator.py ties evaluate_supplier_reliability() to
its own audit store: the computation stays I/O-free and independently
testable, while this module is the one place that decides what gets
logged and durably recorded, and when.

Every delayed PO the analysis produces a cost for gets its own audit
record, regardless of severity - "outcome" on an audit record means "did
the analysis process succeed for this PO," not "was this PO's delay
severe." A run that produces zero delay costs (no delivery data, or
every delivery was on time) still gets one run-level audit record
instead of none, and if analyze_shipment_delays() itself cannot run at
all (a bad parameter, or an unexpected crash), a run-level failure
record is written the same way - "audit trail missing for shipment
analysis" must not happen for a completed run of any outcome.
Run-level records use `po_id=None` (see audit_trail.py), not a sentinel
string, so a real PO id can never collide with one.

If the audit store itself can't be written to (disk full, permissions),
that failure is never silently swallowed, but it also never masks the
more useful signal when the analysis itself failed first - see
_fail_run()'s handling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from shipment_delay_analysis.audit_trail import ShipmentDelayAuditStore, ShipmentDelayAuditWriteError
from shipment_delay_analysis.delay_analysis import (
    DEFAULT_COST_PER_DAY_LATE,
    DEFAULT_DELAY_THRESHOLD_DAYS,
    ShipmentDelayCost,
    SupplierDelayPattern,
    analyze_shipment_delays,
)
from shipment_delay_analysis.logging_setup import get_logger

logger = get_logger()

AnalysisRunOutcome = Literal["success", "crashed"]


@dataclass
class ShipmentDelayAnalysisRun:
    """The full outcome of one ShipmentDelayEvaluator.run() call."""

    analysis_id: str
    delay_costs: list[ShipmentDelayCost] = field(default_factory=list)
    patterns: list[SupplierDelayPattern] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    crash_error: str | None = None

    @property
    def outcome(self) -> AnalysisRunOutcome:
        return "crashed" if self.crash_error is not None else "success"

    @property
    def total_cost(self) -> float:
        return sum(c.total_cost for c in self.delay_costs)


class ShipmentDelayEvaluator:
    """Runs analyze_shipment_delays() and audits every result, success or failure."""

    def __init__(self, audit_store: ShipmentDelayAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        rows: list[dict[str, Any]],
        *,
        analysis_id: str | None = None,
        delay_threshold_days: float = DEFAULT_DELAY_THRESHOLD_DAYS,
        cost_per_day_late: float = DEFAULT_COST_PER_DAY_LATE,
    ) -> ShipmentDelayAnalysisRun:
        analysis_id = analysis_id or str(uuid.uuid4())
        logger.info(
            "shipment_delay_analysis_started",
            extra={
                "event": "shipment_delay_analysis_started",
                "correlation_id": analysis_id,
                "context": {"row_count": len(rows)},
            },
        )

        try:
            report = analyze_shipment_delays(
                rows,
                delay_threshold_days=delay_threshold_days,
                cost_per_day_late=cost_per_day_late,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            # Covers both the documented DelayCostError/SupplierDelayError
            # (a bad parameter) and any genuinely unexpected bug in the
            # analysis path - both must not leave the run with no audit
            # trail at all, caught here at the one orchestration boundary,
            # the same way supplier_evaluation/evaluator.py's
            # SupplierEvaluator.run() catches unexpected exceptions at its
            # own orchestration boundary.
            return self._fail_run(analysis_id, exc)

        for cost in report.delay_costs:
            write_error = self._try_record(
                analysis_id=analysis_id,
                po_id=cost.po_id,
                outcome="success",
                delay_days=cost.delay_days,
                severity=cost.severity,
                total_cost=cost.total_cost,
                detail=cost.detail,
            )
            if write_error is not None:
                # The audit store itself is broken - retrying immediately
                # would just raise again (infinite retry loops are
                # prohibited), so the run is reported crashed rather than
                # left with a misleadingly "successful" outcome and a
                # silently incomplete audit trail for the remaining POs.
                return ShipmentDelayAnalysisRun(analysis_id=analysis_id, crash_error=str(write_error))
            logger.info(
                "shipment_delay_analyzed",
                extra={
                    "event": "shipment_delay_analyzed",
                    "outcome": "success",
                    "correlation_id": analysis_id,
                    "context": {
                        "po_id": cost.po_id,
                        "delay_days": cost.delay_days,
                        "severity": cost.severity,
                        "total_cost": cost.total_cost,
                    },
                },
            )

        if not report.delay_costs:
            # No PO was delayed enough to cost out (no delivery data, or
            # every delivery was on time) - the analysis process still ran
            # and completed, so it still needs an audit trace. Without
            # this, a completed run with zero delays is indistinguishable
            # from one that never ran at all.
            reason = "; ".join(report.warnings) if report.warnings else "no shipment delays found"
            write_error = self._try_record(analysis_id=analysis_id, po_id=None, outcome="success", detail=reason)
            if write_error is not None:
                return ShipmentDelayAnalysisRun(analysis_id=analysis_id, crash_error=str(write_error))

        run = ShipmentDelayAnalysisRun(
            analysis_id=analysis_id,
            delay_costs=report.delay_costs,
            patterns=report.patterns,
            warnings=report.warnings,
        )
        logger.info(
            "shipment_delay_analysis_completed",
            extra={
                "event": "shipment_delay_analysis_completed",
                "outcome": "success",
                "correlation_id": analysis_id,
                "context": {
                    "delays_analyzed": len(run.delay_costs),
                    "suppliers_with_patterns": len(run.patterns),
                    "total_cost": run.total_cost,
                },
            },
        )
        return run

    def _try_record(self, **kwargs: Any) -> ShipmentDelayAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except ShipmentDelayAuditWriteError as exc:
            logger.error(
                "shipment_delay_audit_write_failed",
                extra={
                    "event": "shipment_delay_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("analysis_id"),
                    "context": {"po_id": kwargs.get("po_id")},
                },
            )
            return exc

    def _fail_run(self, analysis_id: str, exc: Exception) -> ShipmentDelayAnalysisRun:
        logger.error(
            "shipment_delay_analysis_failed",
            extra={
                "event": "shipment_delay_analysis_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": analysis_id,
                "context": {},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller (it names the real
        # root cause of the failed analysis; a failed audit write on top
        # of that is a second, already-logged problem), so it is never
        # replaced or masked by an audit-store exception.
        self._try_record(
            analysis_id=analysis_id,
            po_id=None,
            outcome="failure",
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return ShipmentDelayAnalysisRun(analysis_id=analysis_id, crash_error=str(exc))
