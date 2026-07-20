from eve_risk.analysis import FleetAnalyzer
from eve_risk.report import ReportRenderer, build_summary


def test_renderer_produces_png(now, identities, ship_types) -> None:
    report = FleetAnalyzer().analyze(
        request_id="report-job",
        requested_count=2,
        identities=identities,
        invalid_names=[],
        killmails=[],
        ship_types=ship_types,
        covered_character_ids={1, 2},
        now=now,
    )
    image = ReportRenderer(width=1440, max_height=4096).render(report)
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    summary = build_summary(report)
    assert "有效 2/2" in summary
    assert "覆盖 100%" in summary
    assert "敌对威胁指数" in summary
