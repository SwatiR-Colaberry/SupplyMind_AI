import pytest

from data_console.raw_query_validator import UnsafeQueryError, validate_read_only_query


def test_a_plain_select_passes_through_trimmed():
    assert validate_read_only_query("  SELECT a, b FROM t  ") == "SELECT a, b FROM t"


def test_a_select_with_a_join_passes():
    query = "SELECT a.x, b.y FROM a JOIN b ON a.id = b.a_id"
    assert validate_read_only_query(query) == query


def test_a_cte_starting_with_with_passes():
    query = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    assert validate_read_only_query(query) == query


def test_a_single_trailing_semicolon_is_stripped():
    assert validate_read_only_query("SELECT 1;") == "SELECT 1"


def test_empty_string_is_rejected():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query("")


def test_whitespace_only_is_rejected():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query("   \n  ")


def test_a_second_statement_after_a_semicolon_is_rejected():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query("SELECT 1; DROP TABLE orders")


def test_not_starting_with_select_or_with_is_rejected():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query("DELETE FROM orders")


def test_a_leading_comment_is_rejected_rather_than_stripped():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query("-- notes\nSELECT * FROM orders")


@pytest.mark.parametrize("keyword", ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "GRANT", "COPY", "INTO", "PG_SLEEP"])
def test_blocked_keywords_are_rejected_even_mid_query(keyword):
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query(f"SELECT * FROM t WHERE 1=1 {keyword}")


def test_a_column_name_containing_a_blocked_word_as_a_substring_is_not_rejected():
    # Word-boundary matching: "copyright_year" contains "copy" but isn't
    # the keyword COPY, so it must not trip the guardrail.
    query = "SELECT copyright_year FROM t"
    assert validate_read_only_query(query) == query
