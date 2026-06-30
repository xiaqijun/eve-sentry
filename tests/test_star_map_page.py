from app.server.star_map_page import INDEX_HTML


def test_star_map_page_includes_runtime_config_panel():
    assert "Scoring Config" in INDEX_HTML
    assert 'id="cfg-whitelist"' in INDEX_HTML
    assert 'id="cfg-blacklist"' in INDEX_HTML
    assert 'id="cfg-corps"' in INDEX_HTML
    assert 'id="cfg-alliances"' in INDEX_HTML
    assert 'id="cfg-standing"' in INDEX_HTML
    assert 'id="cfg-cooldown"' in INDEX_HTML
    assert 'fetch("/api/config"' in INDEX_HTML
    assert 'method: "PUT"' in INDEX_HTML


def test_star_map_page_includes_manual_intel_form():
    assert "Manual Intel" in INDEX_HTML
    assert 'id="obs-system"' in INDEX_HTML
    assert 'id="obs-names"' in INDEX_HTML
    assert 'id="obs-source"' in INDEX_HTML
    assert 'id="obs-raw"' in INDEX_HTML
    assert 'id="obs-submit"' in INDEX_HTML
    assert 'fetch("/api/observations"' in INDEX_HTML
    assert "function submitObservation()" in INDEX_HTML


def test_star_map_page_includes_alert_evidence_view():
    assert 'id="tab-alerts"' in INDEX_HTML
    assert "function renderAlerts()" in INDEX_HTML
    assert "snapshot.alerts" in INDEX_HTML
    assert "Score ${Number(alert.score || 0)}" in INDEX_HTML
    assert "evidence-item" in INDEX_HTML
    assert 'alert.names.join(", ")' in INDEX_HTML
