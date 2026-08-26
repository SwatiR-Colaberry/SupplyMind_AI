import pytest

from agents.contracts import AgentFinding, AgentResponse, ResponseValidationError, validate_response


def test_validate_response_accepts_a_well_formed_ok_response():
    response = AgentResponse(agent_name="a", status="ok", recommendation="Reorder now", confidence=0.7)

    assert validate_response(response) is response


def test_agent_response_findings_defaults_to_empty_list():
    # An agent written before AgentFinding existed (or with nothing
    # per-subject to report, e.g. DemandForecastingAgent) must still
    # construct a valid AgentResponse without knowing about `findings`.
    response = AgentResponse(agent_name="a", status="ok", recommendation="Reorder now", confidence=0.7)

    assert validate_response(response) is response
    assert response.findings == []


def test_agent_response_accepts_explicit_findings():
    finding = AgentFinding(subject="SKU-123", subject_kind="sku", severity="high", detail="days_of_supply=2.1")
    response = AgentResponse(
        agent_name="a", status="ok", recommendation="Reorder SKU-123", confidence=0.7, findings=[finding]
    )

    assert validate_response(response) is response
    assert response.findings == [finding]


def test_validate_response_accepts_a_well_formed_error_response():
    response = AgentResponse(agent_name="a", status="error", error="upstream data unavailable")

    assert validate_response(response) is response


def test_validate_response_rejects_wrong_type():
    with pytest.raises(ResponseValidationError, match="expected AgentResponse"):
        validate_response({"status": "ok", "recommendation": "Reorder"})


def test_validate_response_rejects_ok_with_empty_recommendation():
    response = AgentResponse(agent_name="a", status="ok", recommendation="   ")

    with pytest.raises(ResponseValidationError, match="non-empty recommendation"):
        validate_response(response)


def test_validate_response_rejects_out_of_range_confidence():
    response = AgentResponse(agent_name="a", status="ok", recommendation="Reorder", confidence=1.5)

    with pytest.raises(ResponseValidationError, match="out of range"):
        validate_response(response)


def test_validate_response_rejects_error_with_no_message():
    response = AgentResponse(agent_name="a", status="error", error=None)

    with pytest.raises(ResponseValidationError, match="missing an error message"):
        validate_response(response)


def test_validate_response_rejects_unrecognized_status():
    # AgentResponseStatus is typed as Literal["ok", "error"], but Python
    # doesn't enforce that at runtime - an agent implementation could
    # still hand back any string.
    response = AgentResponse(agent_name="a", status="pending", recommendation="Reorder")

    with pytest.raises(ResponseValidationError, match="unknown status"):
        validate_response(response)
