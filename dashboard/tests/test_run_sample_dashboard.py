from __future__ import annotations

import dashboard.live_refresh as live_refresh
import dashboard.run_sample_dashboard as run_sample_dashboard


def test_datasets_and_refresh_real_data_dashboard_are_still_importable_from_here():
    # chat_interface/run_sample_chat_interface.py imports DATASETS (plus the
    # SYNTHETIC_* rows still defined in this module) from here - this
    # module re-exports it from dashboard.live_refresh, where the shared
    # real_data pipeline now actually lives, so that import must keep
    # working unchanged.
    assert run_sample_dashboard.DATASETS is live_refresh.DATASETS
    assert run_sample_dashboard.refresh_real_data_dashboard is live_refresh.refresh_real_data_dashboard
