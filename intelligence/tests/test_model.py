import pytest

from intelligence.audit_trail import StageAuditStore
from intelligence.model import STAGE_ORDER, IntelligenceModel


def _model(tmp_path) -> IntelligenceModel:
    return IntelligenceModel(StageAuditStore(tmp_path / "audit.jsonl"))


def _increasing_rows(months: int = 8) -> list[dict]:
    return [{"order_date": f"2025-{m:02d}-15", "quantity": 100 + 10 * m} for m in range(1, months + 1)]


# --- happy path: acceptance criteria 1 and 2 ---


def test_run_processes_raw_rows_through_all_four_stages(tmp_path):
    model = _model(tmp_path)

    run = model.run(_increasing_rows(), run_id="run-happy")

    assert [r.stage for r in run.results] == list(STAGE_ORDER)
    assert all(r.outcome == "success" for r in run.results)
    assert run.outcome == "success"


def test_observe_stage_provides_observations_from_raw_inputs(tmp_path):
    model = _model(tmp_path)

    run = model.run(_increasing_rows(4), run_id="run-observe")

    assert run.observation is not None
    assert run.observation.row_count == 4
    assert len(run.observation.points) == 4
    assert run.observation.period_range == ("2025-01", "2025-04")


def test_understand_stage_provides_insights_derived_from_observations(tmp_path):
    model = _model(tmp_path)

    run = model.run(_increasing_rows(8), run_id="run-understand")

    assert run.understanding is not None
    assert run.understanding.trend_direction == "increasing"
    assert any("trend is increasing" in insight for insight in run.understanding.insights)


def test_predict_and_recommend_stages_produce_a_forecast_backed_action(tmp_path):
    model = _model(tmp_path)

    run = model.run(_increasing_rows(8), run_id="run-predict")

    assert run.prediction is not None
    assert len(run.prediction.forecast.points) == 3  # default periods_ahead
    assert run.recommendation is not None
    assert "increase stock" in run.recommendation.action


def test_low_confidence_forecast_recommends_gathering_more_data(tmp_path):
    model = _model(tmp_path)
    # Oscillating demand a linear trend fits poorly (confidence ~0.2 < threshold).
    rows = [
        {"order_date": f"2025-{m:02d}-15", "quantity": q}
        for m, q in zip(range(1, 5), [100, 200, 100, 200])
    ]

    run = model.run(rows, run_id="run-low-confidence")

    assert run.recommendation.action == "collect more historical data before acting on this forecast"


# --- failure path: "data not processed through all stages" ---


def test_pipeline_halts_at_observe_when_no_raw_data_is_given(tmp_path):
    model = _model(tmp_path)

    run = model.run([], run_id="run-empty")

    outcomes = {r.stage: r.outcome for r in run.results}
    assert outcomes["observe"] == "failure"
    assert outcomes["understand"] == "not_processed"
    assert outcomes["predict"] == "not_processed"
    assert outcomes["recommend"] == "not_processed"
    assert run.outcome == "failure"
    assert run.understanding is None
    assert run.prediction is None
    assert run.recommendation is None


def test_pipeline_halts_at_observe_when_rows_are_missing_required_fields(tmp_path):
    model = _model(tmp_path)
    # Rows present but none carry the expected date/quantity keys - distinct
    # from the "no rows at all" case: aggregation runs, yields zero usable
    # points, and observe must still fail rather than hand understand an
    # empty Observation.
    rows = [{"customer_id": 1}, {"customer_id": 2}]

    run = model.run(rows, run_id="run-missing-fields")

    outcomes = {r.stage: r.outcome for r in run.results}
    assert outcomes["observe"] == "failure"
    assert "no usable rows" in run.results[0].error
    assert outcomes["understand"] == "not_processed"
    assert run.outcome == "failure"


def test_pipeline_halts_at_predict_when_history_is_too_short(tmp_path):
    model = _model(tmp_path)
    # A single row aggregates to a single period - forecast_demand needs >= 2.
    rows = [{"order_date": "2025-01-15", "quantity": 100}]

    run = model.run(rows, run_id="run-short-history")

    outcomes = {r.stage: r.outcome for r in run.results}
    assert outcomes["observe"] == "success"
    assert outcomes["understand"] == "success"
    assert outcomes["predict"] == "failure"
    assert outcomes["recommend"] == "not_processed"
    assert run.outcome == "failure"
    assert run.recommendation is None


# --- trust: audit trail of model stages ---


def test_every_stage_gets_an_audit_record_on_success(tmp_path):
    audit_store = StageAuditStore(tmp_path / "audit.jsonl")
    model = IntelligenceModel(audit_store)

    model.run(_increasing_rows(), run_id="run-audited")

    recorded_stages = {r.stage: r.outcome for r in audit_store.records_for_run("run-audited")}
    assert recorded_stages == {"observe": "success", "understand": "success", "predict": "success", "recommend": "success"}


def test_every_stage_gets_an_audit_record_even_when_the_pipeline_halts_early(tmp_path):
    audit_store = StageAuditStore(tmp_path / "audit.jsonl")
    model = IntelligenceModel(audit_store)

    model.run([], run_id="run-halted")

    recorded_stages = {r.stage: r.outcome for r in audit_store.records_for_run("run-halted")}
    assert recorded_stages == {
        "observe": "failure",
        "understand": "not_processed",
        "predict": "not_processed",
        "recommend": "not_processed",
    }


def test_rerunning_the_same_run_id_does_not_duplicate_audit_records(tmp_path):
    audit_store = StageAuditStore(tmp_path / "audit.jsonl")
    model = IntelligenceModel(audit_store)

    model.run(_increasing_rows(), run_id="run-rerun")
    model.run(_increasing_rows(), run_id="run-rerun")

    assert len(audit_store.records_for_run("run-rerun")) == 4


def test_run_id_defaults_to_a_generated_value_when_not_supplied(tmp_path):
    model = _model(tmp_path)

    run = model.run(_increasing_rows())

    assert run.run_id  # non-empty, generated
