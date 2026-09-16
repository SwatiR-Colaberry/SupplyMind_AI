import re

from learn import visuals


def _fill_rect_width(svg: str) -> float:
    # The 2nd <rect> is always the fill/value rect in these SVGs (the 1st
    # is the track/first segment).
    widths = re.findall(r'<rect[^>]*width="([\d.]+)"', svg)
    return float(widths[1])


def test_render_meter_svg_is_valid_svg_with_an_aria_label():
    html = visuals.render_meter_svg(72, "conf")

    assert html.startswith("<svg")
    assert html.endswith("</svg>")
    assert 'aria-label="72 out of 100"' in html


def test_render_meter_svg_does_not_bake_in_a_caption_text_element():
    # Regression (2026-09-13): a caption baked into the SVG's own 220-unit
    # viewBox overflowed and got silently clipped for longer sentences - a
    # user reported "some values are getting cut." Callers now render the
    # caption as a normal HTML element instead (see learn/serve_learn.py).
    html = visuals.render_meter_svg(72, "conf")

    assert "<text" not in html


def test_render_meter_svg_gives_the_fill_rect_a_stable_id_for_live_updates():
    html = visuals.render_meter_svg(72, "conf-meter")

    assert 'id="conf-meter-fill"' in html


def test_render_meter_svg_clamps_out_of_range_values():
    over = visuals.render_meter_svg(500, "over")
    under = visuals.render_meter_svg(-50, "under")

    assert _fill_rect_width(over) <= 220.0
    assert _fill_rect_width(under) >= 0.0


def test_render_timeline_svg_shows_placed_and_arrives_labels_only():
    html = visuals.render_timeline_svg("lead-time")

    assert "Order placed" in html
    assert "Arrives" in html
    assert "<text" in html  # the 2 short tick labels are fine inside the SVG
    assert html.count("<text") == 2


def test_render_split_bar_svg_shows_both_segment_labels_with_stable_ids():
    html = visuals.render_split_bar_svg("Everyday stock", 80, "Safety buffer", "safety-stock")

    assert "Everyday stock" in html
    assert "Safety buffer" in html
    assert html.count("<rect") == 2
    assert 'id="safety-stock-left"' in html
    assert 'id="safety-stock-right"' in html
    assert 'id="safety-stock-left-label"' in html
    assert 'id="safety-stock-right-label"' in html


def test_render_split_bar_svg_escapes_its_labels():
    html = visuals.render_split_bar_svg("<script>alert(1)</script>", 50, "safe", "x")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_scale_svg_marks_the_value_and_shows_the_typical_band():
    html = visuals.render_scale_svg(2.19, "z-score")

    assert "<circle" in html
    assert 'id="z-score-marker"' in html
    assert "<text" not in html
