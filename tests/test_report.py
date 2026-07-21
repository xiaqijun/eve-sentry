import io

from PIL import Image

from eve_risk.analysis import FleetAnalyzer
from eve_risk.domain import ZKillStats
from eve_risk.report import ReportAssets, ReportRenderer, _footer_text, build_summary


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
    report.lifetime_stats = ZKillStats(
        character_id=1,
        ships_destroyed=1099,
        ships_lost=37,
        points_destroyed=2132,
        isk_destroyed=202_342_000_000,
        isk_lost=3_640_000_000,
        solo_kills=40,
        danger_ratio=92,
        gang_ratio=97,
    )
    image = ReportRenderer(width=1440, max_height=4096).render(report)
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    rendered = Image.open(io.BytesIO(image))
    assert rendered.width == 1440
    assert 2500 <= rendered.height <= 4096
    summary = build_summary(report)
    assert "有效 2/2" in summary
    assert "覆盖 100%" in summary
    assert "敌对威胁指数" in summary


def test_renderer_accepts_portrait_and_ship_assets(now, identities, ship_types) -> None:
    identity = identities[0].model_copy(
        update={
            "corporation_ticker": "ALPHA",
            "alliance_ticker": "ONE",
            "birthday": now,
            "security_status": 4.1,
        }
    )
    report = FleetAnalyzer().analyze(
        request_id="asset-report",
        requested_count=1,
        identities=[identity],
        invalid_names=[],
        killmails=[],
        ship_types=ship_types,
        covered_character_ids={1},
        now=now,
    )
    asset = io.BytesIO()
    Image.new("RGB", (64, 64), "#49b6ff").save(asset, format="PNG")

    image = ReportRenderer(width=960).render(
        report,
        ReportAssets(
            character_portraits={1: asset.getvalue()},
            ship_icons={1001: asset.getvalue()},
            corporation_logos={101: asset.getvalue()},
            alliance_logos={201: asset.getvalue()},
        ),
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_historical_report_labels_its_data_window(now, identities, ship_types) -> None:
    report = FleetAnalyzer().analyze(
        request_id="historical-report",
        requested_count=1,
        identities=identities[:1],
        invalid_names=[],
        killmails=[],
        ship_types=ship_types,
        covered_character_ids={1},
        window_days=401,
        now=now,
    )

    assert "数据窗口 历史样本" in _footer_text(report)
