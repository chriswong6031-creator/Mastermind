"""Regression coverage for the portfolio dashboard's user-facing hierarchy."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "static" / "index.html").read_text()
PORTFOLIO_DESK_HTML = (ROOT / "app" / "static" / "portfolio.html").read_text()
MARKET_VIEW_HTML = (ROOT / "app" / "static" / "market_view.html").read_text()
AGENDA_HTML = (ROOT / "app" / "static" / "agenda.html").read_text()
THEME = (ROOT / "app" / "static" / "theme.css").read_text()
THEME_JS = (ROOT / "app" / "static" / "theme.js").read_text()
CHAT = (ROOT / "app" / "static" / "chat.js").read_text()
ACCOUNT = (ROOT / "app" / "static" / "account.js").read_text()


def test_dashboard_omits_macro_readiness_posture_and_provenance_clutter() -> None:
    for removed_id in (
        'id="mm-provenance"',
        'id="readiness-sec"',
        'id="posture-sec"',
        'id="hero"',
        'id="perf-macro"',
    ):
        assert removed_id not in HTML


def test_performance_contains_chart_and_safety_follows_core_activity() -> None:
    performance = HTML.index('id="performance"')
    equity_curve = HTML.index('id="equity-curve"')
    allocation = HTML.index('id="alloc"')
    positions = HTML.index('id="positions"')
    trades = HTML.index('id="trades"')
    safety = HTML.index('id="safety"')
    legend = HTML.index('id="ec-legend"')

    assert performance < equity_curve < allocation < positions < trades < safety
    assert '<span class="l-en">Equity Curve</span>' not in HTML
    assert equity_curve < legend < allocation
    assert 'id="ec-note"' not in HTML


def test_daily_decision_log_is_bounded_and_expandable() -> None:
    assert "var DECISION_PREVIEW_COUNT = 5" in HTML
    assert 'id="dec-more"' in HTML
    assert 'aria-controls="dec-list"' in HTML
    assert "window.toggleDecisionLog" in HTML
    assert "_decisions.slice(0, DECISION_PREVIEW_COUNT)" in HTML


def test_brain_summary_uses_available_width_and_only_discloses_real_overflow() -> None:
    assert "body.page-mm .mm-auto-sum {" in HTML
    auto_summary_rule = HTML.index("body.page-mm .mm-auto-sum {")
    auto_summary_slice = HTML[auto_summary_rule : auto_summary_rule + 180]
    assert "width: 100%" in auto_summary_slice
    assert "max-width: none" in auto_summary_slice

    assert 'aria-controls="auto-sum"' in HTML
    assert "var maxLines = compact ? 4 : 6" in HTML
    assert "sumEl.scrollHeight > (lineHeight * maxLines) + 2" in HTML
    assert "if (!needsDisclosure)" in HTML
    assert "moreEl.style.display = 'none'" in HTML
    assert "window.addEventListener('resize'" in HTML


def test_dashboard_uses_san_francisco_with_inter_fallback() -> None:
    assert "--font-sans:" in THEME
    assert '"SF Pro Text"' in THEME
    assert '"SF Pro Display"' in THEME
    assert "Inter" in THEME
    assert "font-family: var(--font-sans" in HTML
    assert "font-family: var(--font-sans" in PORTFOLIO_DESK_HTML
    assert "fonts.googleapis.com" not in THEME


def test_dashboard_defers_noncritical_analytics_and_prefetches_workspaces() -> None:
    assert "window.setTimeout(function ()" in THEME_JS
    assert "window.requestIdleCallback(loadGA4, { timeout: 2500 })" in THEME_JS
    assert "}, 4000)" in THEME_JS
    for path in ("/portfolio_desk", "/market_view", "/agenda"):
        assert f'<link rel="prefetch" href="{path}" as="document">' not in HTML
        assert f"'{path}'" in HTML
    assert "function _scheduleWorkspacePrefetch()" in HTML
    assert "}, 5000)" in HTML
    for page in (PORTFOLIO_DESK_HTML, MARKET_VIEW_HTML, AGENDA_HTML):
        assert '<link rel="prefetch"' not in page
    assert "}, 6000)" in THEME_JS


def test_portfolio_switches_paint_cached_data_before_live_revalidation() -> None:
    assert "var _portfolioDataCache = {}" in HTML
    assert "function _fetchPortfolioSnapshot(id)" in HTML
    assert "function _schedulePortfolioPrefetch()" in HTML
    assert "var PORTFOLIO_SNAPSHOT_MAX_AGE_MS = 30000" in HTML
    assert "Date.now() - cached.cachedAt > PORTFOLIO_SNAPSHOT_MAX_AGE_MS" in HTML
    assert "var cached = _portfolioDataCache[id]" in HTML
    set_portfolio = HTML.index("window.setPortfolio = function(id)")
    cached_paint = HTML.index("if (cached) _paintPortfolioSnapshot(cached, false)", set_portfolio)
    revalidate = HTML.index("fetchAll({ scopedOnly: true, showBar: !cached })", set_portfolio)
    assert cached_paint < revalidate


def test_hidden_histories_are_lazy_and_dom_bounded() -> None:
    assert "var RUNS_RENDER_CHUNK = 60" in HTML
    assert "var shownRuns = runs.slice(0, _runsVisible)" in HTML
    assert "window.showMoreRuns" in HTML
    assert "var PAPERS_RENDER_CHUNK = 60" in HTML
    assert "var shownPapers = _papers.slice(0, _papersVisible)" in HTML
    assert "window.showMorePapers" in HTML
    assert "if (_currentView === 'research')" in HTML
    assert "_safeRender('researchPapers', renderResearchPapers)" in HTML
    assert "function _ensureRuns()" in HTML
    assert "function _ensureResearchPapers()" in HTML
    assert "function _ensureCalibration()" in HTML


def test_live_marks_replace_around_the_clock_full_dashboard_polling() -> None:
    assert 'id="live-marks-status"' in HTML
    assert "fetch('/api/live_marks?portfolio='" in HTML
    assert "cache: 'no-store'" in HTML
    assert "_scheduleLiveMarks(data.poll_after_seconds)" in HTML
    assert "document.addEventListener('visibilitychange'" in HTML
    assert "setInterval(fetchAll, 60000)" not in HTML
    assert "setInterval(loadPortfolios, 60000)" not in HTML
    assert "if (document.hidden)" in HTML


def test_initial_loadbar_finishes_after_scoped_portfolio_paints() -> None:
    paint = HTML.index("_paintPortfolioSnapshot(scopedSnapshot)")
    done = HTML.index("loadBarDone(); barFinished = true", paint)
    shared = HTML.index("var s = await sharedPromise", paint)
    assert paint < done < shared


def test_institutional_shell_is_shared_across_supporting_workspaces() -> None:
    for page in (PORTFOLIO_DESK_HTML, MARKET_VIEW_HTML, AGENDA_HTML):
        assert 'class="mm-product-bar"' in page
        assert 'class="mm-product-brand"' in page
        assert 'class="mm-product-links"' in page
        assert 'class="mm-page-heading"' in page

    assert ".mm-product-bar" in THEME
    assert ".mm-page-heading" in THEME


def test_every_page_uses_safe_area_aware_mobile_viewport() -> None:
    for page in (HTML, PORTFOLIO_DESK_HTML, MARKET_VIEW_HTML, AGENDA_HTML):
        assert 'viewport-fit=cover' in page


def test_mobile_navigation_fits_without_clipped_horizontal_rails() -> None:
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in THEME
    assert ".mm-product-links a {" in THEME
    assert "min-height: 44px" in THEME

    institutional = HTML.index("/* Command rail: deliberately in document flow.")
    mobile_nav = HTML.index("@media (max-width: 820px)", institutional)
    mobile_slice = HTML[mobile_nav : mobile_nav + 2600]
    assert "body.page-mm .mm-nav-tabs" in mobile_slice
    assert "display: grid" in mobile_slice
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in mobile_slice
    assert "body.page-mm .mm-nav-tab" in mobile_slice
    assert "min-height: 44px" in mobile_slice


def test_mobile_portfolio_switch_reveals_the_active_book() -> None:
    assert "function revealActivePortfolioTab()" in HTML
    assert "host.querySelector('.mm-pf-tab.active')" in HTML
    assert "active.offsetLeft - (host.clientWidth - active.offsetWidth) / 2" in HTML
    assert "requestAnimationFrame(revealActivePortfolioTab)" in HTML


def test_mobile_forms_dialogs_and_tables_are_touch_ready() -> None:
    assert "body.page-mm #mm-thesis-modal" in HTML
    assert "min-height: 100dvh" in HTML
    assert "body.page-mm .mm-sd-unit-btn" in HTML
    assert "min-width: 44px" in HTML
    assert "Swipe horizontally to view the full schedule" in HTML

    assert "Swipe horizontally to view all position fields" in PORTFOLIO_DESK_HTML
    assert "table.pos-table { min-width: 920px; }" in PORTFOLIO_DESK_HTML
    assert "body.page-pf.pf-modal-open { overflow: hidden; }" in PORTFOLIO_DESK_HTML
    assert 'evt.target === evt.currentTarget' in PORTFOLIO_DESK_HTML

    assert "Swipe horizontally to inspect every signal field" in MARKET_VIEW_HTML
    assert "-webkit-overflow-scrolling: touch" in MARKET_VIEW_HTML


def test_mobile_overlays_respect_safe_areas_and_touch_targets() -> None:
    assert "height:100dvh" in CHAT
    assert "#bc-hd button,#bc-sheet .sh-top button{width:44px;height:44px;}" in CHAT
    assert "env(safe-area-inset-bottom)" in CHAT
    assert "#bc-in{min-height:32px;font-size:16px;}" in CHAT

    assert ".mmacc-trigger{width:44px;height:44px}" in ACCOUNT
    assert ".mmacc-x{width:44px;height:44px}" in ACCOUNT
    assert ".mmacc-input{min-height:48px;font-size:16px}" in ACCOUNT
    assert "document.body.classList.add('mmacc-open')" in ACCOUNT


def test_manual_ledger_is_portfolio_focused() -> None:
    for removed in (
        'id="mkt-strip"',
        'id="mkt-state"',
        'id="mkt-radar"',
        'id="pf-alerts-panel"',
        "function loadAlerts",
    ):
        assert removed not in PORTFOLIO_DESK_HTML

    assert "Manual Ledger" in PORTFOLIO_DESK_HTML
    assert "var(--dn)" not in PORTFOLIO_DESK_HTML
    assert "var(--fg)" not in PORTFOLIO_DESK_HTML


def test_flagship_only_views_cannot_leak_into_other_books() -> None:
    assert "var flagship = _portfolio === 'flagship'" in HTML
    assert "if (!flagship && _currentView !== 'dashboard') showView('dashboard')" in HTML
    assert "nd.style.display = flagship ? '' : 'none'" in HTML
    assert "nr.style.display = flagship ? '' : 'none'" in HTML


def test_navigation_remains_in_flow_and_accessible() -> None:
    assert "body.page-mm #mm-nav {" in HTML
    institutional_nav = HTML.index("/* Command rail: deliberately in document flow.")
    nav_rule = HTML.index("body.page-mm #mm-nav {", institutional_nav)
    nav_slice = HTML[nav_rule : nav_rule + 700]
    assert "position: relative" in nav_slice
    assert "position: sticky" not in nav_slice
    assert "position: fixed" not in nav_slice
    assert 'aria-pressed="' in HTML
    assert 'role="combobox"' in HTML
    assert 'role="dialog"' in HTML


def test_market_view_table_has_mobile_overflow_container() -> None:
    assert 'class="mv-table-scroll"' in MARKET_VIEW_HTML
    assert "min-width: 880px" in MARKET_VIEW_HTML


def test_market_view_distinguishes_unconfirmed_from_agreement() -> None:
    assert "LABEL vs PLANES — UNCONFIRMED" in MARKET_VIEW_HTML
    assert "Neutral or balanced evidence is an abstention, not confirmation." in MARKET_VIEW_HTML
    assert "artifact_present === false" in MARKET_VIEW_HTML


def test_hk_security_names_follow_the_active_language() -> None:
    assert "function securityName(row)" in HTML
    assert "(isZh && row.name_zh) ? row.name_zh" in HTML
    assert "String(row.ticker || '').trim().toUpperCase()" in HTML
    assert "var posName = securityName(p)" in HTML
    assert "var orderName = securityName(o)" in HTML
    assert "var tradeName = securityName(r)" in HTML
    assert "var trName = securityName(tr)" in HTML
    assert "var holdingName = securityName(h)" in HTML
    assert "var selfPosName = securityName(p)" in HTML
    assert "var selfTradeName = securityName(r)" in HTML
