"""Repo-wide pytest fixtures.

Currently just one: resetting dashboard.live_refresh's in-process "last
successful Run Analysis snapshot" cache before (and after) every test.
That cache is deliberately a plain module-level global (see
live_refresh.py's own docstring on _latest_real_data_snapshot) - the
right scope for a single long-running dev server process, where any
handler in that process should see the latest "Run Analysis" result. In
a pytest session, though, many unrelated test functions share that same
process: any test that calls refresh_real_data_dashboard() (or
_build_dashboard() for the "real_data" scenario) sets this global, and
without a reset it stays set for every test that happens to run after -
a real leak this repo hit immediately (a chat-snapshot test's own
hand-built fixture snapshot was silently overridden by whatever an
earlier, unrelated live_refresh test had left behind).
"""

import pytest

import dashboard.live_refresh as live_refresh


@pytest.fixture(autouse=True)
def _reset_latest_real_data_snapshot():
    live_refresh._latest_real_data_snapshot = None
    yield
    live_refresh._latest_real_data_snapshot = None
