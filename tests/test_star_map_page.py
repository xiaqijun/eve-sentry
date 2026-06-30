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


def test_star_map_page_includes_esi_status_panel():
    assert "ESI Session" in INDEX_HTML
    assert 'id="esi-status"' in INDEX_HTML
    assert 'id="esi-refresh"' in INDEX_HTML
    assert 'id="esi-use-system"' in INDEX_HTML
    assert 'fetch("/api/esi/status"' in INDEX_HTML
    assert 'fetch("/api/esi/session?location=true&contacts=false"' in INDEX_HTML
    assert "function loadEsiStatus()" in INDEX_HTML
    assert "function useEsiSystem()" in INDEX_HTML
    assert "manualSystemId" in INDEX_HTML
    assert "payload.system_id = manualSystemId" in INDEX_HTML


def test_star_map_page_includes_alert_evidence_view():
    assert 'id="tab-alerts"' in INDEX_HTML
    assert 'id="events-pill"' in INDEX_HTML
    assert "function renderAlerts()" in INDEX_HTML
    assert "snapshot.alerts" in INDEX_HTML
    assert "Score ${Number(alert.score || 0)}" in INDEX_HTML
    assert "evidence-item" in INDEX_HTML
    assert 'alert.names.join(", ")' in INDEX_HTML


def test_star_map_page_subscribes_to_alert_event_stream():
    assert "new EventSource(eventStreamUrl())" in INDEX_HTML
    assert "function connectEventStream()" in INDEX_HTML
    assert "function upsertAlert(alert)" in INDEX_HTML
    assert "eventStream.addEventListener(\"alert\"" in INDEX_HTML
    assert 'new URLSearchParams({ limit: "50", timeout: "30" })' in INDEX_HTML
    assert "const refreshIntervalMs = streaming ? 15000 : 2000" in INDEX_HTML


def test_star_map_page_includes_alert_detail_lookup_view():
    assert "data-alert-details" in INDEX_HTML
    assert "function toggleAlertDetails(alertId)" in INDEX_HTML
    assert "function loadAlertDetails(alert)" in INDEX_HTML
    assert "function fetchOptional(path, key)" in INDEX_HTML
    assert 'fetchOptional(`/api/characters/${query.id}`' in INDEX_HTML
    assert 'fetchOptional(`/api/characters/by-name/${encodeURIComponent(query.name)}`' in INDEX_HTML
    assert 'fetchOptional(`/api/kill-activity/character/${characterId}`' in INDEX_HTML
    assert 'fetchOptional(`/api/systems/by-name/${encodeURIComponent(systemName)}`' in INDEX_HTML
    assert 'fetchOptional(`/api/kill-activity/system/${systemId}`' in INDEX_HTML
    assert "alert-detail" in INDEX_HTML
