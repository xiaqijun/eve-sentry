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


def test_star_map_page_includes_client_status_panel():
    assert "Client Status" in INDEX_HTML
    assert 'id="heartbeat-status"' in INDEX_HTML
    assert 'id="heartbeat-refresh"' in INDEX_HTML
    assert 'fetch("/api/heartbeats"' in INDEX_HTML
    assert "function loadHeartbeats()" in INDEX_HTML
    assert "function renderHeartbeats()" in INDEX_HTML
    assert "function formatHeartbeatSummary(summary, fallbackItems = [])" in INDEX_HTML
    assert "payload.summary" in INDEX_HTML
    assert "item.status || \"unknown\"" in INDEX_HTML


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
    assert "function loadEntityIntel(detail)" in INDEX_HTML
    assert "function fetchOptional(path, key)" in INDEX_HTML
    assert 'fetchOptional(`/api/alerts/${encodeURIComponent(alertId)}`' in INDEX_HTML
    assert "/api/intel/character/" in INDEX_HTML
    assert "/api/intel/system/" in INDEX_HTML
    assert "/api/intel/corporation/" in INDEX_HTML
    assert "/api/intel/alliance/" in INDEX_HTML
    assert "Degraded Sources" in INDEX_HTML
    assert "Related Intel" in INDEX_HTML
    assert "/api/characters/" not in INDEX_HTML
    assert "/api/kill-activity/character/" not in INDEX_HTML
    assert "alert-detail" in INDEX_HTML


def test_star_map_page_scales_map_from_snapshot_bounds():
    assert 'id="map-header"' in INDEX_HTML
    assert 'id="map-fit"' in INDEX_HTML
    assert 'id="map-zoom"' in INDEX_HTML
    assert "function mapBounds()" in INDEX_HTML
    assert "const bounds = mapBounds();" in INDEX_HTML
    assert "const headerBottom = headerRect ? Math.max(0, headerRect.bottom - mapRect.top) : 0;" in INDEX_HTML
    assert "const topPadding = Math.max(40, Math.ceil(headerBottom + 24));" in INDEX_HTML
    assert "const innerWidth = Math.max(1, rect.width - bounds.padLeft - bounds.padRight);" in INDEX_HTML
    assert "const xRatio = bounds.spanX <= 0 ? 0.5" in INDEX_HTML
    assert "const yRatio = bounds.spanY <= 0 ? 0.5" in INDEX_HTML
    assert "system.x / 1000 * rect.width" not in INDEX_HTML
    assert "system.y / 700 * rect.height" not in INDEX_HTML


def test_star_map_page_supports_pan_zoom_and_fit():
    assert "const viewport = { zoom: 1, panX: 0, panY: 0 };" in INDEX_HTML
    assert "function fitMap()" in INDEX_HTML
    assert "function hitTestSystem(point)" in INDEX_HTML
    assert "function selectSystemAtPoint(point)" in INDEX_HTML
    assert "function clampZoom(value)" in INDEX_HTML
    assert "function updateZoomLabel()" in INDEX_HTML
    assert "canvas.addEventListener(\"wheel\"" in INDEX_HTML
    assert "canvas.addEventListener(\"pointerdown\"" in INDEX_HTML
    assert "canvas.addEventListener(\"pointermove\"" in INDEX_HTML
    assert "canvas.addEventListener(\"pointerup\"" in INDEX_HTML
    assert "fitMapButton.addEventListener(\"click\"" in INDEX_HTML
    assert "viewport.panX = pointX - (pointX - viewport.panX) * ratio;" in INDEX_HTML
    assert "suppressClick = pointerDrag.moved;" in INDEX_HTML
    assert "if (!pointerDrag.moved) {" in INDEX_HTML
    assert "selectSystemAtPoint(point);" in INDEX_HTML
    assert "const labelWidth = ctx.measureText(system.name).width;" in INDEX_HTML
