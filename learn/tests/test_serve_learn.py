from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from dashboard.render import GLOSSARY
from learn.serve_learn import LearnHandler, _meter_band, _meter_caption, _scale_caption, _timeline_caption, render_page


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), LearnHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(base_url: str, path: str = "/"):
    try:
        resp = urllib.request.urlopen(base_url + path)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_get_root_serves_the_learn_page(server):
    status, body = _get(server, "/")

    assert status == 200
    assert b"<h1>Glossary</h1>" in body


def test_get_root_page_includes_the_shared_first_visit_onboarding_overlay(server):
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<div class="onboarding-overlay" id="onboarding-overlay">' in text


def test_get_root_page_has_a_card_for_every_glossary_term():
    # Every dashboard.render.GLOSSARY term gets its own explainer card
    # here - this is the "different page ... in more details ... with
    # some charts and images" a user asked for, after pointing out the
    # in-app glossary panels only cover one line per term.
    html = render_page()

    for term in GLOSSARY:
        assert f'<div class="explainer-term">{term.capitalize()}</div>' in html


def test_get_root_page_includes_an_svg_visual_per_card():
    html = render_page()

    # Count <svg> only inside .explainer-card divs, since the page also
    # has non-explainer <svg> icons of its own (the page header's badge,
    # the 3 icons in the shared first-visit onboarding overlay).
    card_start = html.index('<div class="explainer-grid">')
    cards_html = html[card_start:]
    assert cards_html.count("<svg") == len(GLOSSARY)


def test_get_root_page_uses_the_shared_nav_bar_with_glossary_current(server):
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert text.count('class="nav-item') == 5
    assert '<span class="nav-item nav-current">' in text
    assert "Glossary</span>" in text


def test_get_unknown_path_is_404(server):
    status, _ = _get(server, "/nope")

    assert status == 404


def test_get_root_page_no_svg_contains_a_text_element():
    # Regression (2026-09-13): every visual used to draw its own caption
    # as an SVG <text> element sized in the 220-unit viewBox's own
    # coordinate space - longer captions ran past the viewBox and were
    # silently clipped (a default root <svg>'s overflow is 'hidden'), a
    # user reported as "some values are getting cut." Captions now render
    # as normal HTML next to the SVG - assert per-card <svg>...</svg>
    # blocks only ever contain the 2 short structural labels the timeline
    # and split-bar visuals draw (which are proportionally sized and
    # never overflow), never a long caption sentence.
    import re

    html = render_page()
    for svg_block in re.findall(r"<svg.*?</svg>", html, flags=re.DOTALL):
        for text_match in re.findall(r"<text[^>]*>([^<]*)</text>", svg_block):
            assert len(text_match) < 20, f"unexpectedly long SVG text (clipping risk): {text_match!r}"


def test_get_root_page_gives_every_visual_card_a_slider():
    # Added 2026-09-13 after a user asked for "some dynamic ideas" -
    # every card is now interactive, not just a static picture.
    html = render_page()

    assert html.count('class="explainer-slider"') == len(GLOSSARY)


def test_get_root_page_meter_and_scale_and_timeline_cards_have_a_caption_div():
    html = render_page()

    for slug in ("confidence", "risk-score", "z-score", "stockout-probability", "lead-time", "on-time-rate"):
        assert f'<div class="explainer-caption-text" id="{slug}-caption">' in html


def test_meter_band_direction_aware():
    # Confidence/on-time rate: higher is better ("positive"). Risk
    # score/stockout probability: higher is worse ("negative").
    assert _meter_band(80, "positive") == "strong"
    assert _meter_band(80, "negative") == "high"
    assert _meter_band(10, "positive") == "low"
    assert _meter_band(10, "negative") == "low"


def test_meter_caption_matches_the_page_js_mirror_wording():
    assert _meter_caption(72, "positive") == "72 out of 100 - strong"
    assert _meter_caption(34, "negative") == "34 out of 100 - moderate"


def test_scale_caption_flags_outside_the_typical_range():
    assert "outside the typical range" in _scale_caption(2.19)
    assert "within the typical range" in _scale_caption(0.5)


def test_timeline_caption_reports_days():
    assert _timeline_caption(10) == "10 day(s) from order to arrival"
