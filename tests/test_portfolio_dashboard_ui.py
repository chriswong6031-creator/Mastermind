"""Regression coverage for the portfolio dashboard's user-facing hierarchy."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "static" / "index.html").read_text()
PORTFOLIO_DESK_HTML = (ROOT / "app" / "static" / "portfolio.html").read_text()
MARKET_VIEW_HTML = (ROOT / "app" / "static" / "market_view.html").read_text()
AGENDA_HTML = (ROOT / "app" / "static" / "agenda.html").read_text()
THEME = (ROOT / "app" / "static" / "theme.css").read_text()


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


def test_institutional_shell_is_shared_across_supporting_workspaces() -> None:
    for page in (PORTFOLIO_DESK_HTML, MARKET_VIEW_HTML, AGENDA_HTML):
        assert 'class="mm-product-bar"' in page
        assert 'class="mm-product-brand"' in page
        assert 'class="mm-product-links"' in page
        assert 'class="mm-page-heading"' in page

    assert ".mm-product-bar" in THEME
    assert ".mm-page-heading" in THEME


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
