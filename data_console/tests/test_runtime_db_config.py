from unittest.mock import patch

import pytest

from data_console import runtime_db_config
from data_console.runtime_db_config import clear_runtime_config, get_postgres_config, set_runtime_config
from data_integration.config import MissingConfigError, PostgresConfig

_CONFIG = PostgresConfig(host="h", port=5432, database="d", user="u", password="p")


@pytest.fixture(autouse=True)
def reset_override():
    """Every test starts and ends with no runtime override set - this module's
    state is a bare process global, so a test that sets one and forgets to
    clear it would silently leak into every test that runs after it."""
    clear_runtime_config()
    yield
    clear_runtime_config()


def test_get_postgres_config_falls_back_to_env_when_no_override_is_set():
    with patch("data_console.runtime_db_config.load_postgres_config", return_value=_CONFIG) as mock_load:
        assert get_postgres_config() == _CONFIG
    mock_load.assert_called_once()


def test_get_postgres_config_raises_when_neither_override_nor_env_is_available():
    with patch("data_console.runtime_db_config.load_postgres_config", side_effect=MissingConfigError("not set")):
        with pytest.raises(MissingConfigError):
            get_postgres_config()


def test_set_runtime_config_takes_priority_over_the_environment():
    with patch("data_console.runtime_db_config.load_postgres_config") as mock_load:
        set_runtime_config(_CONFIG)
        assert get_postgres_config() == _CONFIG
    mock_load.assert_not_called()


def test_clear_runtime_config_reverts_to_the_environment_path():
    set_runtime_config(_CONFIG)
    clear_runtime_config()

    with patch("data_console.runtime_db_config.load_postgres_config", side_effect=MissingConfigError("not set")):
        with pytest.raises(MissingConfigError):
            get_postgres_config()


def test_the_override_is_never_written_to_a_module_level_constant_by_import():
    # Sanity check that the module's default state really is "no override" -
    # a regression here would mean the fixture above is masking a bug rather
    # than proving one doesn't exist.
    assert runtime_db_config._runtime_override is None
