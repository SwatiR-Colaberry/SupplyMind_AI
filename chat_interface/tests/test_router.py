from __future__ import annotations

import pytest

from chat_interface.router import TOPIC_DESCRIPTIONS, classify_query


@pytest.mark.parametrize(
    "query_text,expected_topic",
    [
        ("What is the demand forecast for next month?", "demand_forecasting_agent"),
        ("Any risk of a stockout this week?", "stockout_risk_agent"),
        ("How reliable is our supplier Acme Freight?", "supplier_evaluation_agent"),
        ("Which shipments are delayed?", "shipment_delay_analysis_agent"),
        ("How's our data quality looking?", "data_quality_monitoring_agent"),
        ("What do you recommend we do?", "recommendation_agent"),
        ("Any anomalies or risk in the supply chain?", "risk_detection_agent"),
    ],
)
def test_classify_query_matches_expected_topic(query_text: str, expected_topic: str) -> None:
    assert classify_query(query_text) == expected_topic


def test_classify_query_is_case_insensitive() -> None:
    assert classify_query("WHAT IS OUR STOCKOUT RISK?") == "stockout_risk_agent"


def test_classify_query_prefers_more_specific_topic_over_generic_risk() -> None:
    # "risk" alone would match risk_detection_agent, but "stockout risk"
    # must resolve to the more specific stockout_risk_agent instead -
    # "stockout" is the longer, more specific matching keyword.
    assert classify_query("what's my stockout risk right now") == "stockout_risk_agent"
    assert classify_query("how risky is our supplier base") == "supplier_evaluation_agent"


def test_classify_query_prefers_longest_match_when_two_specific_topics_collide() -> None:
    # Regression test: an earlier version of classify_query() picked
    # whichever topic happened to be listed first in _TOPIC_KEYWORDS,
    # so this query resolved to supplier_evaluation_agent purely because
    # of table order, not because it was the better answer. The longest
    # matching keyword ("shipment delay", 14 chars) must win over a
    # shorter one ("supplier", 8 chars) regardless of ordering.
    assert classify_query("is our supplier causing shipment delays?") == "shipment_delay_analysis_agent"


def test_classify_query_returns_none_on_a_genuine_cross_topic_length_tie() -> None:
    # Regression test: even after the longest-match fix, two *different*
    # topics tying for the best length ("supplier" and "shipment" are both
    # 8 characters) used to silently fall back to table order, which is
    # exactly the original bug under a new name. A genuine tie must read
    # as unsupported/ambiguous instead of a confident-looking guess.
    assert classify_query("our supplier and shipment carrier update") is None
    assert classify_query("what is the forecast for stockout items") is None


def test_classify_query_tie_within_same_topic_still_resolves() -> None:
    # "shipment delay" and "delivery delay" are both 14 characters and
    # both belong to shipment_delay_analysis_agent - a length tie WITHIN
    # one topic is not ambiguous and must still resolve normally; only a
    # tie ACROSS different topics should return None.
    assert classify_query("we had a shipment delay and a delivery delay") == "shipment_delay_analysis_agent"


def test_classify_query_returns_none_for_unsupported_query() -> None:
    assert classify_query("what's the weather like today?") is None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_classify_query_returns_none_for_blank_query(blank) -> None:
    assert classify_query(blank) is None


def test_topic_descriptions_cover_every_classifiable_topic() -> None:
    # Every topic classify_query() can return must have a human-readable
    # description available, or the "unsupported query" message (built
    # from TOPIC_DESCRIPTIONS by chat.py) would silently omit a topic that
    # actually works.
    from chat_interface.router import _TOPIC_KEYWORDS

    for topic, _ in _TOPIC_KEYWORDS:
        assert topic in TOPIC_DESCRIPTIONS
