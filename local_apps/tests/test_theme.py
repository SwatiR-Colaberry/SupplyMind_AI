import pytest

import local_apps.urls as app_urls
from local_apps import theme


def test_render_nav_bar_marks_the_current_page_as_a_non_link_span():
    html = theme.render_nav_bar("AI Assistant")

    # Regression (2026-09-13): each item now also wraps a small per-tab
    # icon (see .nav-icon in TOKENS_CSS) between the opening tag and the
    # label, so the assertion checks the label sits directly before its
    # closing tag rather than a full literal `>{label}<` match.
    assert '<span class="nav-item nav-current">' in html
    assert "AI Assistant</span>" in html
    assert f'<a class="nav-item" href="{app_urls.CHAT_UI_URL}">AI Assistant</a>' not in html


def test_render_nav_bar_links_to_the_other_destinations():
    html = theme.render_nav_bar("AI Assistant")

    assert f'href="{app_urls.DATA_CONSOLE_URL}">' in html and "Data Console</a>" in html
    assert f'href="{app_urls.LIVE_DASHBOARD_URL}">' in html and "Live Data</a>" in html
    assert f'href="{app_urls.SCENARIO_SIMULATOR_URL}">' in html and "What-If Simulator</a>" in html
    assert f'href="{app_urls.LEARN_URL}">' in html and "Glossary</a>" in html


def test_render_nav_bar_always_shows_exactly_five_items_regardless_of_current_page():
    # Grew from 4 to 5 (2026-09-13) with the addition of the /learn/
    # screen, labeled "Glossary" (renamed from "Learn" after a user asked
    # for "something more meaningful").
    for label in ("Data Console", "Live Data", "AI Assistant", "What-If Simulator", "Glossary"):
        html = theme.render_nav_bar(label)
        assert html.count('class="nav-item') == 5


def test_render_nav_bar_rejects_an_unknown_label():
    with pytest.raises(ValueError, match="Nonexistent Page"):
        theme.render_nav_bar("Nonexistent Page")


def test_render_nav_bar_accepts_none_and_links_all_five():
    # A page that is none of the 5 fixed destinations (e.g. one of the 2
    # demo dashboard scenarios) still gets the full nav, just with
    # nothing marked current.
    html = theme.render_nav_bar(None)

    assert html.count('class="nav-item') == 5
    assert "nav-current" not in html


def test_tokens_css_defines_the_shared_palette_and_nav_chrome():
    assert "--brand: #1d4ed8;" in theme.TOKENS_CSS
    assert ".nav-bar {" in theme.TOKENS_CSS
    assert ".nav-current { background: var(--brand); color: white; }" in theme.TOKENS_CSS


def test_tokens_css_defines_the_page_header_accent_and_empty_state_treatments():
    # Added 2026-09-12 for the "professional but interesting" visual-polish
    # pass - a second accent color, a background-treatment band for page
    # headers, and a designed empty-state card in place of plain text.
    assert "--accent-2:" in theme.TOKENS_CSS
    assert ".page-header {" in theme.TOKENS_CSS
    assert ".page-header-icon {" in theme.TOKENS_CSS
    assert ".empty-state {" in theme.TOKENS_CSS


def test_render_page_header_wraps_the_icon_and_body_together():
    html = theme.render_page_header(theme.ICON_DATABASE, "<h1>Data Console</h1>")

    assert '<div class="page-header">' in html
    assert '<div class="page-header-icon page-header-icon-accent-2">' in html
    assert theme.ICON_DATABASE in html
    assert '<div class="page-header-body"><h1>Data Console</h1></div>' in html


def test_render_page_header_accepts_an_explicit_accent():
    # Added 2026-09-12 after a user asked to "add more colors" - every
    # screen's icon badge used to be the same flat purple regardless of
    # which screen it was; each screen now picks its own accent.
    html = theme.render_page_header(theme.ICON_DATABASE, "<h1>x</h1>", accent="brand")

    assert '<div class="page-header-icon page-header-icon-brand">' in html


def test_render_page_header_rejects_an_unknown_accent():
    with pytest.raises(ValueError, match="bogus"):
        theme.render_page_header(theme.ICON_DATABASE, "<h1>x</h1>", accent="bogus")


def test_google_font_links_loads_inter_with_a_system_fallback():
    # Added 2026-09-12 after a user asked "is there any other font we can
    # try here" - Inter loaded via Google Fonts, with the exact prior
    # system-font stack still listed as a fallback so a page that can't
    # reach the CDN renders exactly as it did before this change.
    assert "fonts.googleapis.com" in theme.GOOGLE_FONT_LINKS
    assert "Inter" in theme.GOOGLE_FONT_LINKS
    assert '"Inter", -apple-system' in theme.TOKENS_CSS


def test_tokens_css_defines_a_fixed_background_treatment_on_body():
    # Added 2026-09-12 (second polish pass): a user said the pages still
    # looked plain and asked specifically for "a background image which
    # remains static even when we scroll down." Implemented as CSS
    # radial-gradients (no raster asset to host) pinned via
    # background-attachment: fixed, on body's own rule so it applies to
    # every screen that includes TOKENS_CSS.
    assert "background-attachment: fixed;" in theme.TOKENS_CSS
    assert "background-image:" in theme.TOKENS_CSS
    assert "radial-gradient(" in theme.TOKENS_CSS


def test_tokens_css_background_includes_an_on_theme_svg_motif():
    # Added 2026-09-12 (third polish pass): plain gradient blobs alone
    # didn't read as matching "the theme of the app" per the user's
    # follow-up - added a small tiled data: URI SVG (a dashed
    # supply-chain route between nodes plus a crate outline) as the first
    # background-image layer, still no raster asset/new dependency.
    assert "data:image/svg+xml" in theme.TOKENS_CSS
    assert "background-repeat: repeat, no-repeat, no-repeat, no-repeat;" in theme.TOKENS_CSS


def test_nav_bar_css_spans_the_full_page_width():
    # Regression (2026-09-12, third report): the nav bar used to be
    # width: fit-content (a small pill hugging its own links on the left)
    # - a user explicitly asked for "a tab which covers the width of
    # page." Each .nav-item now shares the row equally via flex: 1.
    # (2026-09-13: centering moved from text-align to flex/align-items
    # once each item gained its own icon alongside the label text.)
    assert ".nav-bar {" in theme.TOKENS_CSS
    assert "width: 100%;" in theme.TOKENS_CSS
    assert "flex: 1; display: flex; align-items: center; justify-content: center;" in theme.TOKENS_CSS


def test_nav_bar_gives_each_item_an_icon_in_its_own_accent_color():
    # Added 2026-09-13 after a user picked "small per-tab icons (reusing
    # each page's own accent color)" from a set of suggestions for a nav
    # bar a prior screenshot showed as "too plain."
    html = theme.render_nav_bar("Data Console")

    assert html.count('class="nav-icon nav-icon-') == 5
    assert 'class="nav-icon nav-icon-brand">' in html  # Data Console
    assert 'class="nav-icon nav-icon-warning">' in html  # Live Data
    assert 'class="nav-icon nav-icon-success">' in html  # What-If Simulator
    assert html.count('class="nav-icon nav-icon-accent-2">') == 2  # AI Assistant + Glossary


def test_tokens_css_lets_the_current_tabs_icon_inherit_white_instead_of_its_own_accent():
    # The current tab is a solid brand-blue pill with white text - its
    # icon should match (inherit white), not keep its own accent color
    # clashing on top of that background.
    for accent in ("brand", "accent-2", "success", "warning"):
        assert f".nav-current .nav-icon-{accent}" in theme.TOKENS_CSS
    assert "color: inherit;" in theme.TOKENS_CSS


def test_tokens_css_defines_a_term_tooltip_affordance():
    # Added 2026-09-12 (third polish pass) after a user said jargon like
    # "confidence" and "risk score" "does not explain anything" on its
    # own - a shared .term class (dotted underline, cursor: help) marks a
    # term as carrying a native title-attribute tooltip.
    assert ".term { cursor: help;" in theme.TOKENS_CSS


def test_term_tooltip_is_a_custom_styled_card_not_a_native_title_attribute():
    # Regression (2026-09-12, third report): a user asked for the term
    # tooltip to look "similar to one we show in power bi" - a plain
    # native `title` attribute (delayed, unstyled, OS-drawn) can't be
    # styled at all, so this was rebuilt as a CSS-only floating card keyed
    # off a `data-tooltip` attribute instead.
    assert ".term::after" in theme.TOKENS_CSS
    assert "content: attr(data-tooltip);" in theme.TOKENS_CSS
    assert ".term:hover::after" in theme.TOKENS_CSS


def test_render_empty_state_includes_the_icon_title_and_text():
    html = theme.render_empty_state(theme.ICON_EMPTY, "No table selected", "Pick one from the list.")

    assert '<div class="empty-state">' in html
    assert theme.ICON_EMPTY in html
    assert '<div class="empty-state-title">No table selected</div>' in html
    assert '<div class="empty-state-text">Pick one from the list.</div>' in html


def test_render_glossary_panel_no_longer_exists():
    # Regression (2026-09-13): render_glossary_panel() (and its
    # .glossary-panel CSS) was removed once the dedicated Glossary screen
    # (learn/serve_learn.py) existed and every one of its 4 callers
    # stopped using it - a follow-up pointed out the per-page panels
    # "has not been removed from other pages," and once removed, this
    # function became genuinely unused code rather than a live component.
    assert not hasattr(theme, "render_glossary_panel")
    assert ".glossary-panel" not in theme.TOKENS_CSS


def test_onboarding_html_has_3_steps_gated_on_localstorage():
    # A design-review ask for a first-visit walkthrough, since the app
    # otherwise assumes a new user already knows its own structure.
    # Shared across all 5 screens (see each server module's own
    # render_page()) so it only ever shows once per browser regardless of
    # which screen loads first.
    assert theme.ONBOARDING_HTML.count('class="onboarding-step') == 3
    assert "1. Connect your data" in theme.ONBOARDING_HTML
    assert "2. Run analysis" in theme.ONBOARDING_HTML
    assert "3. Ask questions, test scenarios" in theme.ONBOARDING_HTML
    assert "localStorage.getItem(KEY)" in theme.ONBOARDING_HTML
    assert "localStorage.setItem(KEY, '1')" in theme.ONBOARDING_HTML


def test_onboarding_html_uses_self_contained_button_classes():
    # Deliberately not a bare <button> or any one screen's own .btn - this
    # overlay is injected into all 5 screens, and only 2 of them happen to
    # style bare <button> tags at all.
    assert 'class="onboarding-btn" id="onboarding-skip"' in theme.ONBOARDING_HTML
    assert 'class="onboarding-btn onboarding-btn-primary" id="onboarding-next"' in theme.ONBOARDING_HTML
    assert ".onboarding-btn {" in theme.TOKENS_CSS


def test_onboarding_overlay_is_hidden_by_default_in_css():
    assert ".onboarding-overlay {" in theme.TOKENS_CSS
    assert "display: none;" in theme.TOKENS_CSS
    assert ".onboarding-overlay.is-visible { display: flex; }" in theme.TOKENS_CSS
