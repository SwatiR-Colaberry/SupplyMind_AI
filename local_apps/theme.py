"""Shared design tokens and top-level nav chrome for the local SupplyMind AI dev UIs.

Before this module existed, each of the 4 locally-served screens
(data_console/serve_data_console.py, chat_interface/serve_chat_ui.py,
scenario_simulation/serve_scenario_simulator.py, and the dashboard/
control_tower_*.html pages via dashboard/render.py) built its own CSS
from scratch. data_console and dashboard/render.py each hand-copied the
same `--brand`/`--ink`/`--surface`/etc. token palette (drift risk: a third
copy the next edit would have to keep in sync by hand); chat_interface
and scenario_simulation instead used their own, unrelated hardcoded-hex
mini palette (`#f5f6f8`, `#1565c0`, ...) that never matched the token
palette at all - which is exactly why those two screens visually read as
a different application from the other two, a real gap a user flagged
(2026-09-11) after the nav-bar link fix made cross-navigation work but
did nothing about the mismatched look on arrival.

This module is the single source of truth for both problems:
- TOKENS_CSS: the one copy of the `:root {...}` custom-property palette
  plus the base chrome every screen shares (font, canvas background,
  nav-bar/nav-item rules, page-header/empty-state treatments) - every
  screen's own <style> block now starts from this instead of redefining
  it.
- render_page_header() / ICON_*: a subtle gradient-plus-dot-grid
  background band and a colored icon badge for each screen's own header
  (kicker/h1/meta content is untouched and stays fully page-specific -
  only wrapped in this shared band+icon), plus a matching `.empty-state`
  treatment for a placeholder message that used to just be plain text.
  Added 2026-09-12 after a user asked for a more "professional but
  interesting" look and specifically for something in the background -
  scoped deliberately to a visual-polish pass (background treatment,
  icons, a second accent color, designed empty states), not a structural
  redesign, per that conversation.
- body's own background-image (below): a second, later same-day pass
  after the user saw the first one and said it still looked plain and
  asked specifically for "a background image which remains static even
  when we scroll" - three soft, fixed-position radial-gradient blobs
  using background-attachment: fixed, so they sit behind the page instead
  of scrolling with it. Sits behind every screen since it lives on body's
  own rule, not a per-page override.
- A third same-day round: the user said it "looks the same" (most likely
  because nothing here takes effect until the running server is
  restarted - these are server-rendered templates, not hot-reloaded
  assets) and asked for a background image that specifically "matches the
  theme of the app," a full-width nav bar ("a tab which covers the width
  of page"), and larger, more appropriate font sizes throughout. The
  background gained a small tiled SVG motif (a dashed supply-chain route
  between 3 nodes plus a crate/package outline) layered under the same 3
  gradient blobs, still a data: URI (no raster asset/new dependency) and
  still fixed - a literal, on-theme pattern rather than abstract color
  blobs alone. `.nav-bar`/`.nav-item` changed from `width: fit-content`
  (a small pill hugging its own content on the left) to `width: 100%`
  with each `.nav-item` at `flex: 1; text-align: center` - true full-width
  tabs, evenly sharing the page's own width. Every `font-size` across this
  module and the 4 screens' own `<style>` blocks was bumped one step up a
  fixed scale (see this same day's PROGRESS.md entry for the full
  before/after table) - small badge/label text moved ~1.5px, body/table
  text ~1.5-2px, headings ~1px - a uniform legibility pass rather than a
  handful of one-off tweaks, so the type scale still reads as one
  consistent hierarchy afterward. A `.term` class (dotted underline,
  `cursor: help`) was added for the plain-language tooltips now attached
  to "confidence" and "risk score" wherever those terms are shown (see
  dashboard/render.py, chat_interface/serve_chat_ui.py, and
  scenario_simulation/serve_scenario_simulator.py) - the user said those
  words "does not explain anything" on their own.
- render_nav_bar(): the one function that builds the top-level nav bar's
  markup, so the same 4 destinations (in the same order, with the same
  "current page" treatment) appear on every screen, including the
  dashboard/control_tower_real_data.html page via dashboard/live_refresh.py's
  own _nav_links(). An earlier revision of this module deliberately let
  the dashboard pages keep a richer 6-item nav (the 4 shared destinations
  plus 2 demo-scenario cross-links folded in) on the theory that they're
  views of one screen, not drift - a user flagged that exact 6-vs-4
  mismatch again afterwards ("Live Data shows more tabs"), so that theory
  was wrong: the shared nav must stay a fixed 4 items everywhere, full
  stop. The 2 demo scenarios (control_tower_partial_failure.html /
  control_tower_synthetic_healthy.html) briefly got their own secondary
  "Demo dashboards: ..." link row instead of being removed outright - the
  user then asked directly whether those demo pages were still needed at
  all now that real data is connected, and the answer was no. Removed
  from this module entirely (not just hidden): render_demo_links_row()
  and the 2 URLs it pointed at are gone. The 2 scenarios themselves still
  exist as acceptance-criteria regression proof - see
  dashboard/run_sample_dashboard.py's own module docstring - just no
  longer linked from anywhere a real user would see.
"""

from __future__ import annotations

import local_apps.urls as app_urls

# Inserted into each of the 4 screens' own <head>, before their <style>
# block - a <link> tag can't live inside TOKENS_CSS itself, since that
# string is only ever placed inside a <style> element. `display=swap`
# means text renders immediately in the fallback stack and swaps to Inter
# once it loads, rather than an invisible-text flash while waiting.
GOOGLE_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
)

TOKENS_CSS = """
  :root {
    --ink: #131826;
    --ink-soft: #4a5468;
    --ink-faint: #8891a1;
    --surface: #ffffff;
    --canvas: #f2f4f8;
    --border: #e1e5ec;
    --border-soft: #edeff3;
    --brand: #1d4ed8;
    --brand-dark: #1638a6;
    --brand-tint: #eaf0fd;
    --success: #0f7b52;
    --success-tint: #e6f4ec;
    --warning: #a85c00;
    --warning-strong: #b5540f;
    --warning-tint: #fbf0dd;
    --neutral: #5f6673;
    --neutral-tint: #eef0f3;
    --danger: #9a2b1e;
    --danger-tint: #fdecea;
    --danger-border: #f3c6c1;
    /* Second accent, used sparingly (icon badges only, so far) so the
       page stays "one brand color, calm" at a glance while still having
       a spot of distinct color - not a second thing competing with
       --brand for attention. */
    --accent-2: #7c3aed;
    --accent-2-tint: #f4eefd;
    --shadow: 0 1px 2px rgba(19, 24, 38, 0.05), 0 1px 8px rgba(19, 24, 38, 0.04);
  }
  * { box-sizing: border-box; }
  body {
    /* "Inter" first (loaded via GOOGLE_FONT_LINKS in each page's <head>,
       a user's own "is there any other font we can try" ask) - a widely-
       used, highly-legible UI typeface at small sizes (Linear, GitHub,
       Notion all ship it) that still reads as "enterprise tool," not
       "marketing site." Every name after it is the exact same OS-native
       fallback stack this page always had, so a page that can't reach
       Google Fonts (offline, blocked) still renders identically to before
       this change - nothing regresses, it only upgrades when available. */
    font-family: "Inter", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 15px;
    color: var(--ink); margin: 0; min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    background-color: var(--canvas);
    /* First layer is a small (160x160) tiled SVG motif - a dashed route
       linking 3 small nodes plus a crate/package outline, at very low
       opacity - a literal supply-chain/logistics theme rather than
       generic abstract shapes, per the user's "should match the theme of
       the app." The 3 radial-gradients behind it are the same corner
       color wash from the prior pass, kept for overall depth. Both are
       data: URIs, not a hosted image file - no new dependency, nothing
       that can 404. */
    background-image:
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160' viewBox='0 0 160 160'%3E%3Cg fill='none' stroke='%231d4ed8' stroke-opacity='0.07' stroke-width='1.6'%3E%3Cpath d='M10 42 H58 M10 42 V118 M10 118 H50' stroke-dasharray='4 4'/%3E%3Ccircle cx='10' cy='42' r='3.2' fill='%231d4ed8' fill-opacity='0.10' stroke='none'/%3E%3Ccircle cx='58' cy='42' r='3.2' fill='%237c3aed' fill-opacity='0.10' stroke='none'/%3E%3Ccircle cx='10' cy='118' r='3.2' fill='%230f7b52' fill-opacity='0.10' stroke='none'/%3E%3Crect x='96' y='88' width='22' height='22' rx='2'/%3E%3Cpath d='M96 99 H118 M107 88 V110'/%3E%3C/g%3E%3C/svg%3E"),
      radial-gradient(ellipse 820px 620px at 12% -8%, rgba(29, 78, 216, 0.10), transparent 60%),
      radial-gradient(ellipse 760px 560px at 100% 8%, rgba(124, 58, 237, 0.09), transparent 60%),
      radial-gradient(ellipse 680px 820px at 46% 108%, rgba(15, 123, 82, 0.06), transparent 55%);
    background-repeat: repeat, no-repeat, no-repeat, no-repeat;
    background-attachment: fixed;
  }
  /* Regression (2026-09-12, third report): the nav bar used to be
     width: fit-content - a small pill hugging its own 4 links on the
     left - and the user asked for "a tab which covers the width of
     page." Each .nav-item now shares the full row equally instead of
     sizing to its own text. */
  .nav-bar {
    display: flex; gap: 6px; margin-bottom: 16px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 5px; box-shadow: var(--shadow); width: 100%;
  }
  .nav-item {
    flex: 1; display: flex; align-items: center; justify-content: center; gap: 7px;
    font-size: 14.5px; font-weight: 600; padding: 9px 14px; border-radius: 7px; text-decoration: none;
    color: var(--ink-soft);
  }
  a.nav-item:hover { background: var(--brand-tint); color: var(--brand-dark); }
  .nav-current { background: var(--brand); color: white; }
  /* A small per-tab icon in that screen's own accent color - a design-
     review ask, since a flat text-only nav bar read as "too plain."
     Reuses the same icon set/accent tokens render_page_header() already
     draws each screen's own header badge from, so a tab visually
     previews the color a user is about to land on. */
  .nav-icon { display: flex; flex-shrink: 0; }
  .nav-icon svg { width: 15px; height: 15px; }
  .nav-icon-brand { color: var(--brand); }
  .nav-icon-accent-2 { color: var(--accent-2); }
  .nav-icon-success { color: var(--success); }
  .nav-icon-warning { color: var(--warning-strong); }
  /* The current tab is a solid brand-blue pill with white text - an
     icon keeping its own accent color on top of that would clash, so it
     falls back to `inherit` (picking up .nav-current's own `color:
     white`) instead. */
  .nav-current .nav-icon-brand, .nav-current .nav-icon-accent-2,
  .nav-current .nav-icon-success, .nav-current .nav-icon-warning { color: inherit; }
  /* A dotted-underline "hover for an explanation" affordance for jargon
     (confidence, risk score, ...) - see dashboard/render.py's GLOSSARY,
     serve_chat_ui.py, serve_scenario_simulator.py, and serve_data_console.py's
     own uses of it, added after a user said those terms "does not
     explain anything" on their own.
     Regression (2026-09-12, third report): the tooltip itself was a plain
     native `title` attribute - functional, but a user asked for something
     closer to the small floating card Power BI shows on hover, not the
     browser's own delayed, unstyled OS tooltip box. Rebuilt as a CSS-only
     custom tooltip: the definition now lives in a `data-tooltip` attribute
     (read via `content: attr(data-tooltip)` below) instead of `title`, so
     every caller that builds a `.term` span needs `data-tooltip="..."` in
     place of `title="..."` - `content: attr()` only ever renders as plain
     text, so no escaping discipline changes for callers that already
     escaped their definition text for an HTML attribute. */
  .term { cursor: help; border-bottom: 1px dotted var(--ink-faint); position: relative; }
  .term::after {
    content: attr(data-tooltip);
    position: absolute; left: 50%; bottom: calc(100% + 10px); transform: translateX(-50%) translateY(4px);
    background: var(--surface); color: var(--ink); border: 1px solid var(--border);
    padding: 10px 14px; border-radius: 8px; font-size: 12.5px; font-weight: 500; line-height: 1.5;
    white-space: normal; width: max-content; max-width: 260px; text-align: left;
    box-shadow: 0 8px 24px rgba(19, 24, 38, 0.18);
    z-index: 60; pointer-events: none; opacity: 0; visibility: hidden;
    transition: opacity 0.12s ease, transform 0.12s ease;
  }
  .term::before {
    content: ''; position: absolute; left: 50%; bottom: calc(100% + 5px);
    width: 10px; height: 10px; background: var(--surface); border-right: 1px solid var(--border);
    border-bottom: 1px solid var(--border); transform: translateX(-50%) rotate(45deg);
    z-index: 59; pointer-events: none; opacity: 0; visibility: hidden; transition: opacity 0.12s ease;
  }
  .term:hover::after, .term:hover::before { opacity: 1; visibility: visible; }
  .term:hover::after { transform: translateX(-50%) translateY(0); }
  /* A shared "facts as a real list" treatment - moved here from
     dashboard/render.py (2026-09-12) after a user said the AI Assistant's
     answer and the What-If Simulator's result "is not in a list ... check
     this for all the pages" - dashboard/render.py's own tile headlines
     already used this exact class pair; sharing it here is what lets
     chat_interface/serve_chat_ui.py reuse dashboard.render.render_answer_lines()
     and render identically, rather than a second near-duplicate CSS rule
     living in a 3rd screen's own <style> block. Visible disc bullets
     (not list-style: none) are deliberate - the ask was for something
     that visually reads as a list, not just one fact per line. */
  .tile-headline { font-size: 14px; color: var(--ink-soft); margin: 0 0 6px; line-height: 1.5; list-style: disc; padding-left: 20px; }
  .headline-line { margin-bottom: 4px; }
  .headline-line:last-child { margin-bottom: 0; }
  .page-header {
    position: relative; display: flex; align-items: center; gap: 16px;
    padding: 22px 26px 20px; margin-bottom: 22px; border-radius: 16px; border: 1px solid var(--border-soft);
    background:
      radial-gradient(circle at 1px 1px, rgba(29, 78, 216, 0.08) 1px, transparent 0) 0 0 / 18px 18px,
      linear-gradient(135deg, var(--brand-tint) 0%, var(--surface) 65%);
  }
  .page-header-icon {
    flex: 0 0 auto; width: 44px; height: 44px; border-radius: 12px;
    background: var(--accent-2-tint); color: var(--accent-2); box-shadow: var(--shadow);
    display: flex; align-items: center; justify-content: center;
  }
  /* Every screen's icon badge used to be the same flat purple regardless
     of which screen it was - a user asked to "add more colors in the
     application." Each screen now gets its own color from the existing
     token palette (see render_page_header's `accent` parameter) instead
     of introducing new hues. */
  .page-header-icon-brand { background: var(--brand-tint); color: var(--brand); }
  .page-header-icon-accent-2 { background: var(--accent-2-tint); color: var(--accent-2); }
  .page-header-icon-success { background: var(--success-tint); color: var(--success); }
  .page-header-icon-warning { background: var(--warning-tint); color: var(--warning-strong); }
  .page-header-icon svg { width: 22px; height: 22px; }
  .page-header-body { flex: 1; min-width: 0; }
  .page-header-body .kicker, .page-header-body h1, .page-header-body .meta { margin: 0; }
  .page-header-body .kicker { margin-bottom: 8px; }
  .page-header-body h1 { margin-bottom: 4px; }
  .empty-state {
    display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;
    padding: 36px 24px; border: 1px dashed var(--border); border-radius: 12px; background: var(--canvas);
    color: var(--ink-faint);
  }
  .empty-state-icon {
    width: 40px; height: 40px; border-radius: 10px; background: var(--neutral-tint); color: var(--ink-faint);
    display: flex; align-items: center; justify-content: center; margin-bottom: 12px;
  }
  .empty-state-icon svg { width: 20px; height: 20px; }
  .empty-state-title { font-size: 15px; font-weight: 650; color: var(--ink-soft); margin-bottom: 4px; }
  .empty-state-text { font-size: 14px; line-height: 1.5; max-width: 380px; }
  /* Shared styling for the small SVG "explainer" visuals learn/visuals.py
     builds (meters, timelines, split bars, scales) - used on the Learn
     page itself and reused verbatim (as a hand-kept-in-sync JS copy,
     same tradeoff as this file's own inline .term tooltips) by
     scenario_simulation/serve_scenario_simulator.py's own live risk-score
     meter, so both places draw from one visual language instead of two. */
  .explainer-svg { width: 100%; height: auto; display: block; }
  .explainer-caption { font-size: 11px; fill: var(--ink-faint); font-weight: 600; }
  .explainer-tick { font-size: 10px; fill: var(--ink-faint); font-weight: 600; }
  /* A first-visit walkthrough (see ONBOARDING_HTML below) - a design-
     review ask, since the app otherwise assumes a new user already knows
     its own structure (connect data, then run analysis, then ask
     questions or simulate). Self-contained button classes rather than a
     bare <button> selector or any one screen's own .btn - this overlay
     is injected into all 5 screens, only 2 of which (chat, simulator)
     happen to style bare <button> tags at all, and Data Console/Learn's
     own .btn classes are local to their own files. */
  .onboarding-overlay {
    position: fixed; inset: 0; background: rgba(19, 24, 38, 0.5); z-index: 200;
    display: none; align-items: center; justify-content: center; padding: 20px;
  }
  .onboarding-overlay.is-visible { display: flex; }
  .onboarding-card {
    background: var(--surface); border-radius: 16px; padding: 28px 30px; max-width: 420px; width: 100%;
    box-shadow: 0 20px 60px rgba(19, 24, 38, 0.28);
  }
  .onboarding-step { display: none; }
  .onboarding-step.is-active { display: block; }
  .onboarding-icon {
    width: 44px; height: 44px; border-radius: 12px; background: var(--brand-tint); color: var(--brand);
    display: flex; align-items: center; justify-content: center; margin-bottom: 14px;
  }
  .onboarding-icon svg { width: 22px; height: 22px; }
  .onboarding-title { font-size: 18px; font-weight: 700; margin: 0 0 8px; color: var(--ink); }
  .onboarding-text { font-size: 14.5px; color: var(--ink-soft); line-height: 1.6; margin: 0 0 22px; }
  .onboarding-footer { display: flex; align-items: center; justify-content: space-between; }
  .onboarding-dots { display: flex; gap: 6px; }
  .onboarding-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--border); }
  .onboarding-dot.is-active { background: var(--brand); }
  .onboarding-actions { display: flex; gap: 8px; }
  .onboarding-btn {
    padding: 8px 16px; border-radius: 7px; font-size: 14px; font-weight: 600; cursor: pointer;
    font-family: inherit; border: 1px solid var(--border); background: var(--surface); color: var(--ink-soft);
  }
  .onboarding-btn:hover { background: var(--canvas); }
  .onboarding-btn-primary { border-color: var(--brand); background: var(--brand); color: white; }
  .onboarding-btn-primary:hover { background: var(--brand-dark); border-color: var(--brand-dark); }
""".strip("\n")

# Small inline stroke-icon set (same viewBox/stroke conventions as
# data_console/serve_data_console.py's own pre-existing connect-option
# icons) for page-header badges and empty states. Kept here rather than
# per-page so every screen's icon reads as part of one consistent visual
# language instead of 4 independently-drawn styles.
ICON_DATABASE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"></ellipse>'
    '<path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5"></path>'
    '<path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"></path></svg>'
)
ICON_CHAT = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9'
    'L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z">'
    '</path></svg>'
)
ICON_SIMULATOR = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M9 2v6L4 18a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-5-10V2"></path>'
    '<path d="M9 2h6"></path><path d="M8.5 13h7"></path></svg>'
)
ICON_GLOSSARY = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M12 6.5c-1.7-1.6-4.3-2-8-1.5v13c3.7-0.5 6.3-0.1 8 1.5c1.7-1.6 4.3-2 8-1.5v-13'
    'c-3.7-0.5-6.3-0.1-8 1.5z"></path><path d="M12 6.5v13"></path></svg>'
)
# A supply-chain-specific "nothing here yet" icon (a small warehouse
# outline) - added after a user asked for empty states to match "the
# theme of the app" rather than a generic shape. See ICON_EMPTY (a plain
# inbox tray) below, kept for callers where "empty" isn't specifically
# about a missing data source.
ICON_WAREHOUSE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M3 10 12 4l9 6"></path><path d="M4 10v10h16V10"></path>'
    '<path d="M9 20v-6h6v6"></path></svg>'
)
ICON_PULSE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>'
)
ICON_EMPTY = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M21 8v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8"></path>'
    '<path d="M3 8l3-5h12l3 5"></path><path d="M3 8h18"></path><path d="M9 12h6"></path></svg>'
)

# A first-visit-only 3-step walkthrough - a design-review ask, since the
# app otherwise assumes a new user already knows its own structure.
# Injected verbatim into all 5 screens' page templates (each screen adds
# it right after <body>, so it overlays regardless of that screen's own
# layout) rather than into just one "entry point" screen, since Data
# Console isn't necessarily the first screen a given user actually opens
# (e.g. a link straight to the AI Assistant). Gated on one shared
# localStorage key so it only ever shows once per browser, no matter
# which of the 5 screens happens to load first - every screen is served
# from the same origin (see local_apps/unified_server.py), so
# localStorage is genuinely shared between them, not per-screen state.
# Pure constant markup, not a function - unlike render_page_header(),
# there is no per-caller content to parametrize.
ONBOARDING_HTML = f"""<div class="onboarding-overlay" id="onboarding-overlay">
  <div class="onboarding-card">
    <div class="onboarding-step is-active" data-step="0">
      <div class="onboarding-icon">{ICON_DATABASE}</div>
      <div class="onboarding-title">1. Connect your data</div>
      <div class="onboarding-text">Start in Data Console - plug in a database, upload a CSV, or link a Google
        Sheet. No coding required.</div>
    </div>
    <div class="onboarding-step" data-step="1">
      <div class="onboarding-icon">{ICON_PULSE}</div>
      <div class="onboarding-title">2. Run analysis</div>
      <div class="onboarding-text">Once your data's connected, click "Run Analysis" to check demand, stockout
        risk, suppliers, and shipments in one pass.</div>
    </div>
    <div class="onboarding-step" data-step="2">
      <div class="onboarding-icon">{ICON_CHAT}</div>
      <div class="onboarding-title">3. Ask questions, test scenarios</div>
      <div class="onboarding-text">Use the AI Assistant to ask plain-language questions about what was found, or
        the What-If Simulator to test a change before committing to it. The Learn page explains any term you're
        not sure about.</div>
    </div>
    <div class="onboarding-footer">
      <div class="onboarding-dots">
        <span class="onboarding-dot is-active" data-dot="0"></span>
        <span class="onboarding-dot" data-dot="1"></span>
        <span class="onboarding-dot" data-dot="2"></span>
      </div>
      <div class="onboarding-actions">
        <button type="button" class="onboarding-btn" id="onboarding-skip">Skip</button>
        <button type="button" class="onboarding-btn onboarding-btn-primary" id="onboarding-next">Next</button>
      </div>
    </div>
  </div>
</div>
<script>
(function() {{
  var KEY = 'supplymind_onboarding_seen';
  var overlay = document.getElementById('onboarding-overlay');
  if (!overlay) return;
  var seen;
  try {{ seen = localStorage.getItem(KEY); }} catch (e) {{ seen = '1'; }}
  if (seen) return;
  overlay.classList.add('is-visible');
  var steps = overlay.querySelectorAll('.onboarding-step');
  var dots = overlay.querySelectorAll('.onboarding-dot');
  var nextBtn = document.getElementById('onboarding-next');
  var skipBtn = document.getElementById('onboarding-skip');
  var current = 0;
  function render() {{
    steps.forEach(function(s, i) {{ s.classList.toggle('is-active', i === current); }});
    dots.forEach(function(d, i) {{ d.classList.toggle('is-active', i === current); }});
    nextBtn.textContent = current === steps.length - 1 ? 'Got it' : 'Next';
  }}
  function dismiss() {{
    overlay.classList.remove('is-visible');
    try {{ localStorage.setItem(KEY, '1'); }} catch (e) {{}}
  }}
  nextBtn.addEventListener('click', function() {{
    if (current === steps.length - 1) {{ dismiss(); return; }}
    current += 1;
    render();
  }});
  skipBtn.addEventListener('click', dismiss);
}})();
</script>"""

# The fixed set of top-level destinations every standalone screen's nav
# bar shows, in the fixed order every screen shows them in - a single
# list so "which screens exist and in what order" is answered once, not
# once per server module. Grew to 5 (2026-09-13) with the addition of
# learn/serve_learn.py - a user asked for the glossary to live on "a
# different page," after separately pointing out the glossary panel
# added to the What-If Simulator was "only accessible in [the] what if
# tab." Labeled "Glossary" (not "Learn," its original label, and not the
# module's own name) after a follow-up asked for "something more
# meaningful" - the page's actual content is a glossary of terms with
# fuller explanations, so the label says exactly that. The underlying
# module/package/URL prefix stay named `learn` (see local_apps/urls.py's
# LEARN_URL, local_apps/unified_server.py's /learn/ prefix) since those
# are internal identifiers a user never sees - renaming only what's
# user-visible avoids an otherwise-pointless file/module rename.
# Each entry's icon/accent matches that screen's own render_page_header()
# call (see each server module's own render_page() - Data Console is
# brand, Live Data is warning, AI Assistant and Glossary are accent-2,
# What-If Simulator is success) - added 2026-09-13 after a user picked
# "small per-tab icons (reusing each page's own accent color)" from a set
# of suggestions for a nav bar a prior round's screenshot called "too
# plain." AI Assistant and Glossary sharing accent-2 mirrors those 2
# screens' own page-header badges already sharing it - not a new
# collision introduced here.
_NAV_DESTINATIONS: list[tuple[str, str, str, str]] = [
    ("Data Console", app_urls.DATA_CONSOLE_URL, "ICON_DATABASE", "brand"),
    ("Live Data", app_urls.LIVE_DASHBOARD_URL, "ICON_PULSE", "warning"),
    ("AI Assistant", app_urls.CHAT_UI_URL, "ICON_CHAT", "accent-2"),
    ("What-If Simulator", app_urls.SCENARIO_SIMULATOR_URL, "ICON_SIMULATOR", "success"),
    ("Glossary", app_urls.LEARN_URL, "ICON_GLOSSARY", "accent-2"),
]


def render_nav_bar(current_label: str | None) -> str:
    """The shared 5-item top nav, with `current_label` rendered as a plain (non-link) span.

    `current_label` must match one of _NAV_DESTINATIONS' labels exactly
    (asserted, not silently ignored - a typo here would otherwise render
    a nav bar with 5 links and no "you are here" indicator at all, the
    same class of bug this module exists to eliminate) - or be None, for
    a page that is none of the 5 (e.g. one of the 2 demo dashboard
    scenarios, still buildable via dashboard/run_sample_dashboard.py but
    no longer linked from anywhere in the running app), in which case all
    5 render as plain links and nothing is marked current.

    Each item carries a small icon in its own accent color (see
    _NAV_DESTINATIONS above) - `.nav-current .nav-icon-*` in TOKENS_CSS
    overrides that color back to `inherit` (picking up `.nav-current`'s
    own `color: white`) so the current tab's icon reads as white-on-brand
    like its label, rather than an odd accent color sitting on top of the
    solid brand-blue pill.
    """
    labels = [label for label, _, _, _ in _NAV_DESTINATIONS]
    if current_label is not None and current_label not in labels:
        raise ValueError(f"current_label {current_label!r} must be one of {labels!r} or None")
    _icons = {
        "ICON_DATABASE": ICON_DATABASE,
        "ICON_PULSE": ICON_PULSE,
        "ICON_CHAT": ICON_CHAT,
        "ICON_SIMULATOR": ICON_SIMULATOR,
        "ICON_GLOSSARY": ICON_GLOSSARY,
    }
    items = []
    for label, url, icon_name, accent in _NAV_DESTINATIONS:
        icon_html = f'<span class="nav-icon nav-icon-{accent}">{_icons[icon_name]}</span>'
        if label == current_label:
            items.append(f'<span class="nav-item nav-current">{icon_html}{label}</span>')
        else:
            items.append(f'<a class="nav-item" href="{url}">{icon_html}{label}</a>')
    return '<div class="nav-bar">\n      ' + "\n      ".join(items) + "\n    </div>"


_PAGE_HEADER_ACCENTS = frozenset({"brand", "accent-2", "success", "warning"})


def render_page_header(icon_svg: str, body_html: str, accent: str = "accent-2") -> str:
    """Wraps a page's own header content in the shared background band + icon badge (see TOKENS_CSS's .page-header/.page-header-icon).

    `body_html` is the page's own kicker/h1/meta markup, built exactly as
    it always was - this function only adds the outer band and icon
    around it, so each screen's own header content (which differs enough
    per screen - e.g. dashboard/render.py's own status badge inline in
    its meta line - that one shared render function handling every
    screen's inner content would be more complex than useful) needs no
    changes beyond being passed in here. `body_html` is trusted,
    caller-built markup (each caller already escapes any real user/data
    value it embeds), not raw user input - same trust boundary
    render_nav_bar's own returned markup already has.

    `accent` (one of _PAGE_HEADER_ACCENTS, default "accent-2" - every
    caller's behavior before this parameter existed) picks the icon
    badge's color from the existing token palette - added after a user
    asked to "add more colors in the application," since every screen's
    badge was the same flat purple regardless of which screen it was.
    """
    if accent not in _PAGE_HEADER_ACCENTS:
        raise ValueError(f"accent {accent!r} must be one of {sorted(_PAGE_HEADER_ACCENTS)!r}")
    return (
        f'<div class="page-header"><div class="page-header-icon page-header-icon-{accent}">{icon_svg}</div>'
        f'<div class="page-header-body">{body_html}</div></div>'
    )


def render_empty_state(icon_svg: str, title: str, text: str) -> str:
    """A designed "nothing here yet" card - see TOKENS_CSS's .empty-state - in place of what used to be a plain text placeholder.

    `title`/`text` are inserted as-is (not escaped here) - every current
    caller passes a fixed string literal, never a data/user value: the
    same trust boundary render_nav_bar's own labels already have. A
    future caller embedding a real value would need to escape it itself
    before calling this, the same discipline chat_interface/serve_chat_ui.py
    already applies to its own template values.
    """
    return (
        f'<div class="empty-state"><div class="empty-state-icon">{icon_svg}</div>'
        f'<div class="empty-state-title">{title}</div><div class="empty-state-text">{text}</div></div>'
    )
