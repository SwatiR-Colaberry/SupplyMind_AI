"""Orchestrates one chat interaction with a durable audit trail (STORY-010 / REQ-016).

Ties answer_query() (chat.py, pure computation) to ChatAuditStore
(audit_trail.py, persistence) the same way data_quality_monitoring/evaluator.py
ties assess_data_quality() to its own audit store: the computation stays
I/O-free and independently testable, while this module is the one place
that decides what gets logged and durably recorded, and when.

Every interaction gets exactly one audit record, regardless of whether it
was answered, unsupported, or data-unavailable - "outcome" on an audit
record means "did the chat interface itself process this query
successfully," not "did the query get a useful answer." If answer_query()
raises unexpectedly (a bug in this call, or - if a future revision wires
in a real API call - "Chat API failure"), a failure record is written the
same way, so "audit trail not recorded for chat interactions" never
happens for a completed call of any outcome. This is the layer that turns
the "Chat interface failure"/"Chat API failure" failure paths from a raw
exception into a typed, audited `crashed` outcome instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from chat_interface.audit_trail import ChatAuditStore, ChatAuditWriteError
from chat_interface.chat import ChatResponse, answer_query
from chat_interface.logging_setup import get_logger
from dashboard.metrics import DashboardSnapshot

logger = get_logger()

ChatInteractionOutcome = Literal["success", "crashed"]


@dataclass
class ChatInteractionRun:
    """The full outcome of one ChatEvaluator.run() call."""

    interaction_id: str
    response: ChatResponse | None = None
    crash_error: str | None = None

    @property
    def outcome(self) -> ChatInteractionOutcome:
        return "crashed" if self.crash_error is not None else "success"


class ChatEvaluator:
    """Runs answer_query() and audits the result, success or crash."""

    def __init__(self, audit_store: ChatAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        query_text: str,
        snapshot: DashboardSnapshot,
        *,
        interaction_id: str | None = None,
    ) -> ChatInteractionRun:
        interaction_id = interaction_id or str(uuid.uuid4())
        logger.info(
            "chat_interaction_started",
            extra={
                "event": "chat_interaction_started",
                "correlation_id": interaction_id,
                "context": {"query_text": query_text},
            },
        )

        try:
            response = answer_query(query_text, snapshot)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            # Covers both the documented ChatQueryError (a bad parameter,
            # e.g. a malformed snapshot) and any genuinely unexpected bug
            # in the answering path - both must not leave the interaction
            # with no audit trail at all, caught here at the one
            # orchestration boundary, the same way every other *Evaluator
            # in this repo catches unexpected exceptions at its own
            # orchestration boundary.
            return self._fail_run(interaction_id, query_text, exc)

        write_error = self._try_record(
            interaction_id=interaction_id,
            outcome="success",
            query_text=query_text,
            status=response.status,
            topic=response.topic,
            answer=response.answer,
        )
        if write_error is not None:
            # The audit store itself is broken - retrying immediately
            # would just raise again (infinite retry loops are
            # prohibited), so the interaction is reported crashed rather
            # than left with a misleadingly "successful" outcome and no
            # durable trace of what was answered.
            return ChatInteractionRun(interaction_id=interaction_id, crash_error=str(write_error))

        logger.info(
            "chat_interaction_completed",
            extra={
                "event": "chat_interaction_completed",
                "outcome": "success",
                "correlation_id": interaction_id,
                "context": {"status": response.status, "topic": response.topic},
            },
        )
        return ChatInteractionRun(interaction_id=interaction_id, response=response)

    def _try_record(self, **kwargs) -> ChatAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except ChatAuditWriteError as exc:
            logger.error(
                "chat_audit_write_failed",
                extra={
                    "event": "chat_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("interaction_id"),
                    "context": {},
                },
            )
            return exc

    def _fail_run(self, interaction_id: str, query_text: str, exc: Exception) -> ChatInteractionRun:
        logger.error(
            "chat_interaction_failed",
            extra={
                "event": "chat_interaction_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": interaction_id,
                "context": {"query_text": query_text},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller (it names the real
        # root cause of the failed interaction; a failed audit write on
        # top of that is a second, already-logged problem), so it is
        # never replaced or masked by an audit-store exception.
        self._try_record(
            interaction_id=interaction_id,
            outcome="failure",
            query_text=query_text,
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return ChatInteractionRun(interaction_id=interaction_id, crash_error=str(exc))
