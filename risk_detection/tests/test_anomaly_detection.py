import pytest

from forecasting.demand_model import DemandPoint
from risk_detection.anomaly_detection import (
    MIN_HISTORY_POINTS_FOR_DETECTION,
    AnomalyDetectionError,
    detect_demand_spikes,
)


def test_detect_demand_spikes_flags_an_obvious_spike():
    history = [
        DemandPoint("2025-01", 100),
        DemandPoint("2025-02", 105),
        DemandPoint("2025-03", 95),
        DemandPoint("2025-04", 500),
    ]

    anomalies = detect_demand_spikes(history)

    assert [a.period for a in anomalies] == ["2025-04"]
    assert anomalies[0].direction == "spike"
    assert anomalies[0].severity == "critical"
    assert anomalies[0].z_score == pytest.approx(97.98, abs=0.01)


def test_detect_demand_spikes_flags_a_drop():
    history = [
        DemandPoint("2025-01", 500),
        DemandPoint("2025-02", 505),
        DemandPoint("2025-03", 495),
        DemandPoint("2025-04", 50),
    ]

    anomalies = detect_demand_spikes(history)

    assert [a.period for a in anomalies] == ["2025-04"]
    assert anomalies[0].direction == "drop"
    assert anomalies[0].severity == "critical"
    assert anomalies[0].z_score < 0


def test_detect_demand_spikes_ignores_ordinary_month_to_month_variation():
    # Realistic mild noise around a stable mean (deltas within +/-8 of 1000) -
    # every point's leave-one-out z-score stays under the default threshold.
    deltas = [5, -8, 3, -2, 6, -4, 1, -6, 4, -3, 2, -7]
    history = [DemandPoint(f"2025-{i + 1:02d}", 1000 + d) for i, d in enumerate(deltas)]

    assert detect_demand_spikes(history) == []


def test_detect_demand_spikes_treats_any_deviation_from_a_flat_baseline_as_certain():
    history = [
        DemandPoint("2025-01", 100),
        DemandPoint("2025-02", 100),
        DemandPoint("2025-03", 100),
        DemandPoint("2025-04", 100),
        DemandPoint("2025-05", 500),
    ]

    anomalies = detect_demand_spikes(history)

    assert [a.period for a in anomalies] == ["2025-05"]
    assert anomalies[0].baseline_stdev == 0
    assert anomalies[0].z_score == float("inf")
    assert anomalies[0].severity == "critical"


def test_detect_demand_spikes_does_not_flag_a_perfectly_flat_history():
    history = [DemandPoint(f"2025-{i:02d}", 100) for i in range(1, 5)]

    assert detect_demand_spikes(history) == []


def test_detect_demand_spikes_raises_when_history_is_too_short():
    history = [DemandPoint(f"2025-{i:02d}", 100) for i in range(1, MIN_HISTORY_POINTS_FOR_DETECTION)]

    with pytest.raises(AnomalyDetectionError, match="at least"):
        detect_demand_spikes(history)


def test_detect_demand_spikes_raises_on_empty_history():
    with pytest.raises(AnomalyDetectionError):
        detect_demand_spikes([])


def test_detect_demand_spikes_rejects_a_non_positive_threshold():
    history = [DemandPoint(f"2025-{i:02d}", 100) for i in range(1, 5)]

    with pytest.raises(AnomalyDetectionError, match="z_threshold"):
        detect_demand_spikes(history, z_threshold=0)


def test_detect_demand_spikes_severity_scales_with_how_far_past_the_threshold_the_z_score_is():
    baseline_vals = [98, 99, 100, 101, 102, 100, 99, 101]
    periods = [f"2025-{i + 1:02d}" for i in range(len(baseline_vals))]

    def anomaly_for(quantity: float):
        history = [DemandPoint(p, v) for p, v in zip(periods, baseline_vals)] + [
            DemandPoint("2026-01", quantity)
        ]
        return next(a for a in detect_demand_spikes(history) if a.period == "2026-01")

    assert anomaly_for(103.06).severity == "medium"
    assert anomaly_for(105.51).severity == "high"
    assert anomaly_for(107.96).severity == "critical"


def test_detect_demand_spikes_respects_a_custom_threshold():
    history = [
        DemandPoint("2025-01", 95),
        DemandPoint("2025-02", 100),
        DemandPoint("2025-03", 105),
        DemandPoint("2025-04", 110.21),
    ]

    assert len(detect_demand_spikes(history, z_threshold=2.0)) > 0
    assert detect_demand_spikes(history, z_threshold=3.0) == []


def test_detect_demand_spikes_returns_results_sorted_by_period_regardless_of_input_order():
    flats = [DemandPoint(f"2025-{i:02d}", 100) for i in (1, 2, 3, 5, 6, 7)]
    spike_a = DemandPoint("2025-04", 900)
    spike_b = DemandPoint("2025-08", 800)
    history = [spike_b, spike_a] + flats

    anomalies = detect_demand_spikes(history)

    assert [a.period for a in anomalies] == ["2025-04", "2025-08"]
