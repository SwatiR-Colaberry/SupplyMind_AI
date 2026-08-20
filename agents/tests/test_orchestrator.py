import time
from unittest.mock import patch

from agents.contracts import AgentQuery, AgentResponse
from agents.orchestrator import Orchestrator


class FakeAgent:
    """A controllable agent: returns each response in `responses` in order."""

    def __init__(self, name, responses):
        self.name = name
        self._responses = iter(responses)

    def run(self, query):
        return next(self._responses)


class RaisingAgent:
    name = "raising_agent"

    def run(self, query):
        raise ConnectionError("agent unreachable")


class HangingAgent:
    name = "hanging_agent"

    def run(self, query):
        time.sleep(5)
        return AgentResponse(agent_name=self.name, status="ok", recommendation="too late")


def test_coordinate_dispatches_to_configured_agents_and_returns_success():
    agent = FakeAgent(
        "demand_agent",
        [AgentResponse(agent_name="demand_agent", status="ok", recommendation="Reorder 50 units", confidence=0.9)],
    )

    run = Orchestrator([agent]).coordinate(AgentQuery(text="Should we reorder SKU-123?"))

    assert run.outcome == "success"
    assert len(run.results) == 1
    result = run.results[0]
    assert result.agent_name == "demand_agent"
    assert result.outcome == "success"
    assert result.reevaluated is False
    assert result.response.recommendation == "Reorder 50 units"


def test_coordinate_requests_reevaluation_on_invalid_response_then_recovers():
    invalid = AgentResponse(agent_name="flaky", status="ok", recommendation="")
    valid = AgentResponse(agent_name="flaky", status="ok", recommendation="Reorder now", confidence=0.8)
    agent = FakeAgent("flaky", [invalid, valid])

    with patch("agents.orchestrator.logger.warning") as mock_warning:
        run = Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    result = run.results[0]
    assert result.outcome == "success"
    assert result.reevaluated is True
    assert result.response.recommendation == "Reorder now"

    mock_warning.assert_called_once()
    _, kwargs = mock_warning.call_args
    assert kwargs["extra"]["event"] == "agent_response_invalid"
    assert kwargs["extra"]["context"]["agent"] == "flaky"


def test_coordinate_fails_when_response_still_invalid_after_reevaluation():
    invalid = AgentResponse(agent_name="broken", status="ok", recommendation="")
    agent = FakeAgent("broken", [invalid, invalid])

    run = Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    result = run.results[0]
    assert result.outcome == "failure"
    assert result.reevaluated is True
    assert "invalid" in result.error


def test_coordinate_catches_agent_communication_failure_without_crashing():
    with patch("agents.orchestrator.logger.error") as mock_error:
        run = Orchestrator([RaisingAgent()]).coordinate(AgentQuery(text="q"))

    result = run.results[0]
    assert result.outcome == "failure"
    assert result.error == "agent unreachable"

    mock_error.assert_called_once()
    _, kwargs = mock_error.call_args
    extra = kwargs["extra"]
    assert extra["event"] == "agent_communication_failed"
    assert extra["error_class"] == "ConnectionError"


def test_coordinate_times_out_a_hanging_agent_without_blocking():
    with patch("agents.orchestrator.logger.error") as mock_error:
        start = time.monotonic()
        run = Orchestrator([HangingAgent()], agent_timeout_seconds=0.05).coordinate(AgentQuery(text="q"))
        elapsed = time.monotonic() - start

    assert elapsed < 1.0

    result = run.results[0]
    assert result.outcome == "failure"
    assert "timed out" in result.error

    mock_error.assert_called_once()
    _, kwargs = mock_error.call_args
    extra = kwargs["extra"]
    assert extra["event"] == "agent_timeout"
    assert extra["error_class"] == "TimeoutError"


def test_coordinate_isolates_one_agent_failure_from_the_rest():
    good = FakeAgent(
        "good",
        [AgentResponse(agent_name="good", status="ok", recommendation="Reorder", confidence=0.9)],
    )
    bad = RaisingAgent()

    run = Orchestrator([good, bad]).coordinate(AgentQuery(text="q"))

    outcomes = {r.agent_name: r.outcome for r in run.results}
    assert outcomes == {"good": "success", "raising_agent": "failure"}


def test_coordinate_flags_wide_confidence_spread_as_inconsistent():
    optimistic = FakeAgent(
        "optimistic",
        [AgentResponse(agent_name="optimistic", status="ok", recommendation="Reorder now", confidence=0.9)],
    )
    cautious = FakeAgent(
        "cautious",
        [AgentResponse(agent_name="cautious", status="ok", recommendation="Hold off", confidence=0.2)],
    )

    with patch("agents.orchestrator.logger.warning") as mock_warning:
        run = Orchestrator([optimistic, cautious]).coordinate(AgentQuery(text="q"))

    # Each agent still returned a valid, individually successful response -
    # inconsistency is a cross-agent concern, not a per-agent failure.
    assert {r.outcome for r in run.results} == {"success"}

    # The flag is on the returned object itself, not just in the logs -
    # a caller inspecting run doesn't have to parse log output to see it.
    assert run.outcome == "inconsistent"
    assert "0.70" in run.inconsistency

    mock_warning.assert_called_once()
    _, kwargs = mock_warning.call_args
    extra = kwargs["extra"]
    assert extra["event"] == "agent_responses_inconsistent"
    assert "0.70" in extra["context"]["detail"]


def test_coordinate_does_not_flag_agreeing_agents_as_inconsistent():
    agent_a = FakeAgent(
        "agent_a",
        [AgentResponse(agent_name="agent_a", status="ok", recommendation="Reorder now", confidence=0.85)],
    )
    agent_b = FakeAgent(
        "agent_b",
        [AgentResponse(agent_name="agent_b", status="ok", recommendation="Reorder now", confidence=0.8)],
    )

    with patch("agents.orchestrator.logger.warning") as mock_warning:
        run = Orchestrator([agent_a, agent_b]).coordinate(AgentQuery(text="q"))

    assert run.outcome == "success"
    assert run.inconsistency is None
    mock_warning.assert_not_called()


def test_coordinate_survives_an_unexpected_internal_error():
    agent = FakeAgent(
        "good",
        [AgentResponse(agent_name="good", status="ok", recommendation="Reorder", confidence=0.9)],
    )

    with patch("agents.orchestrator._detect_data_inconsistency", side_effect=RuntimeError("boom")), patch(
        "agents.orchestrator.logger.error"
    ) as mock_error:
        run = Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    assert run.results == []

    mock_error.assert_called_once()
    _, kwargs = mock_error.call_args
    extra = kwargs["extra"]
    assert extra["event"] == "orchestrator_crashed"
    assert extra["error_class"] == "RuntimeError"


def test_coordinate_reports_crashed_even_when_the_exception_has_no_message():
    # str(RuntimeError()) == "" - outcome must still read "crashed", not
    # fall through to "partial" because an empty string is falsy.
    agent = FakeAgent(
        "good",
        [AgentResponse(agent_name="good", status="ok", recommendation="Reorder", confidence=0.9)],
    )

    with patch("agents.orchestrator._detect_data_inconsistency", side_effect=RuntimeError), patch(
        "agents.orchestrator.logger.error"
    ):
        run = Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    assert run.crash_error == ""
    assert run.outcome == "crashed"


def test_coordinate_treats_an_unrecognized_agent_status_as_an_invalid_response():
    unrecognized = AgentResponse(agent_name="odd", status="pending", recommendation="Reorder")
    valid = AgentResponse(agent_name="odd", status="ok", recommendation="Reorder now", confidence=0.8)
    agent = FakeAgent("odd", [unrecognized, valid])

    with patch("agents.orchestrator.logger.warning") as mock_warning:
        run = Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    result = run.results[0]
    assert result.outcome == "success"
    assert result.reevaluated is True

    mock_warning.assert_called_once()
    _, kwargs = mock_warning.call_args
    assert kwargs["extra"]["event"] == "agent_response_invalid"
    assert kwargs["extra"]["error_class"] == "ResponseValidationError"


def test_coordinate_logs_orchestration_lifecycle_in_order():
    agent = FakeAgent(
        "good",
        [AgentResponse(agent_name="good", status="ok", recommendation="Reorder", confidence=0.9)],
    )

    with patch("agents.orchestrator.logger.info") as mock_info:
        Orchestrator([agent]).coordinate(AgentQuery(text="q"))

    events = [call.args[0] for call in mock_info.call_args_list]
    assert events == ["orchestration_started", "agent_response_validated", "orchestration_completed"]
