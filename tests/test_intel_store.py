from app.server.intel_store import IntelStore, StarSystem


def test_add_report_persists_and_snapshot_aggregates(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(
        path,
        systems={"Tama": StarSystem("Tama", 10, 20, "The Citadel", 0.3)},
        links=[],
    )

    report = store.add_report(
        system=" Tama ",
        names=[" Alice ", "Bob", "Alice"],
        source="test",
        seen_at="2026-06-29T12:00:00+00:00",
    )

    assert report.system == "Tama"
    assert report.names == ["Alice", "Bob"]

    reloaded = IntelStore(path, systems={}, links=[])
    snapshot = reloaded.snapshot()

    assert snapshot["summary"]["report_count"] == 1
    assert snapshot["summary"]["hostile_count"] == 2
    assert snapshot["systems"][0]["name"] == "Tama"
    assert snapshot["systems"][0]["hostiles"] == ["Alice", "Bob"]


def test_list_reports_filters_and_limits(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    store.add_report("Tama", ["Alice"], seen_at="2026-06-29T12:00:00+00:00")
    store.add_report("Jita", ["Bob"], seen_at="2026-06-29T12:01:00+00:00")
    store.add_report("Tama", ["Carol"], seen_at="2026-06-29T12:02:00+00:00")

    assert [r["names"][0] for r in store.list_reports(limit=2)] == ["Carol", "Bob"]
    assert [r["names"][0] for r in store.list_reports(system="tama")] == [
        "Carol",
        "Alice",
    ]
    assert [r["system"] for r in store.list_reports(name="bob")] == ["Jita"]


def test_delete_report_removes_and_persists(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])
    report = store.add_report("Tama", ["Alice"])

    assert store.delete_report(report.report_id) is True
    assert store.delete_report(report.report_id) is False
    assert IntelStore(path, systems={}, links=[]).snapshot()["summary"][
        "report_count"
    ] == 0
