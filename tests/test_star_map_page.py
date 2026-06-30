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
