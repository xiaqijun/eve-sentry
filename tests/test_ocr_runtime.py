from app.engine import ocr_runtime


def test_preload_onnx_runtime_imports_code_without_creating_models(monkeypatch):
    imported = []
    monkeypatch.setattr(
        ocr_runtime,
        "_import_onnx_runtime",
        lambda: imported.append("onnx"),
    )

    assert ocr_runtime.preload_ocr_runtime("onnx") is True
    assert imported == ["onnx"]


def test_preload_runtime_leaves_non_onnx_backends_lazy(monkeypatch):
    monkeypatch.setattr(
        ocr_runtime,
        "_import_onnx_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected import")),
    )

    assert ocr_runtime.preload_ocr_runtime("paddle") is False


def test_preload_runtime_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        ocr_runtime,
        "_import_onnx_runtime",
        lambda: (_ for _ in ()).throw(ImportError("missing runtime")),
    )

    assert ocr_runtime.preload_ocr_runtime("onnx") is False
