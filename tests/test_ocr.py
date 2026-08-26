from PIL import Image

from app.engine.ocr import OCREngine


class PredictOCR:
    def __init__(self, raw):
        self.raw = raw
        self.last_shape = None

    def predict(self, img_array):
        self.last_shape = img_array.shape
        return self.raw


class LegacyOCR:
    def __init__(self, raw):
        self.raw = raw
        self.called_with_cls = None

    def ocr(self, img_array, cls=False):
        self.called_with_cls = cls
        return self.raw


class BrokenOCR:
    def predict(self, img_array):
        raise RuntimeError("boom")


def make_engine(fake_ocr, threshold=0.7):
    engine = OCREngine(confidence_threshold=threshold)
    engine._ocr = fake_ocr
    return engine


def test_parse_paddleocr_3_predict_dict_results():
    fake = PredictOCR([
        {
            "rec_texts": [" Alice ", "LowConfidence", "   "],
            "rec_scores": [0.91, 0.2, 0.99],
        }
    ])
    engine = make_engine(fake)

    results = engine.recognize(Image.new("L", (8, 6), color=255))

    assert results == [("Alice", 0.91)]
    assert fake.last_shape == (6, 8, 3)


def test_parse_paddleocr_3_results_with_text_boxes():
    fake = PredictOCR([{
        "rec_texts": [" Alice ", "LowConfidence"],
        "rec_scores": [0.91, 0.2],
        "rec_boxes": [[2, 3, 14, 11], [0, 0, 1, 1]],
    }])
    engine = make_engine(fake)

    results = engine.recognize_with_boxes(Image.new("L", (20, 20), color=255))

    assert results == [("Alice", 0.91, (2, 3, 14, 11))]


def test_parse_paddleocr_2_legacy_ocr_results():
    fake = LegacyOCR([
        [
            [None, ("Bob", 0.95)],
            [None, ["TooLow", 0.3]],
            [None, ("  Carol  ", "0.71")],
        ]
    ])
    engine = make_engine(fake)

    results = engine.recognize(Image.new("RGB", (4, 4), color=(0, 0, 0)))

    assert results == [("Bob", 0.95), ("Carol", 0.71)]
    assert fake.called_with_cls is False


def test_ocr_errors_return_empty_result():
    engine = make_engine(BrokenOCR())

    assert engine.recognize(Image.new("RGB", (4, 4))) == []


def test_initialize_loads_inference_runtime_only_once(monkeypatch):
    engine = OCREngine()
    calls = []

    def initialize(progress=None):
        calls.append(progress)
        engine._ocr = object()

    monkeypatch.setattr(engine, "_init_ocr", initialize)

    assert engine.initialize() is True
    assert engine.initialize() is True
    assert calls == [None]


def test_constructor_falls_back_to_paddleocr_2_option_name():
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        if "device" in kwargs:
            raise TypeError("old PaddleOCR option set")
        return object()

    engine = OCREngine()

    assert engine._create_paddle_ocr(factory, device_arg="gpu:0", use_gpu=True) is not None
    assert calls == [
        {
            "lang": "en",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": "gpu:0",
        },
        {"lang": "en", "use_angle_cls": False, "use_gpu": True},
    ]


def test_constructor_uses_bundled_models(monkeypatch, tmp_path):
    detection_dir = tmp_path / "models" / "PP-OCRv6_medium_det"
    recognition_dir = tmp_path / "models" / "PP-OCRv6_medium_rec"
    detection_dir.mkdir(parents=True)
    recognition_dir.mkdir(parents=True)
    (detection_dir / "inference.pdiparams").write_bytes(b"det")
    (recognition_dir / "inference.pdiparams").write_bytes(b"rec")
    monkeypatch.setattr("app.engine.ocr.sys._MEIPASS", str(tmp_path), raising=False)
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return object()

    engine = OCREngine()

    assert engine._create_paddle_ocr(factory, device_arg="gpu:0", use_gpu=True)
    assert calls[0]["text_detection_model_dir"] == str(detection_dir)
    assert calls[0]["text_recognition_model_dir"] == str(recognition_dir)


def test_default_backend_remains_paddle(monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_OCR_BACKEND", raising=False)

    engine = OCREngine()

    assert engine._backend == "paddle"


def test_onnx_backend_uses_adapter_without_changing_result_shape(monkeypatch, tmp_path):
    calls = []

    class FakeAdapter:
        provider_label = "DirectML GPU"

        def __init__(self, **kwargs):
            calls.append(kwargs)

        def predict(self, _img_array):
            return [{"rec_texts": [" Alice ", "Low"], "rec_scores": [0.98, 0.2]}]

    monkeypatch.setattr("app.engine.onnx_ocr.RapidOCROnnxAdapter", FakeAdapter)
    progress = []
    engine = OCREngine(backend="onnx", model_dir=tmp_path)

    results = engine.recognize(Image.new("RGB", (8, 6)), progress=progress.append)

    assert results == [("Alice", 0.98)]
    assert calls == [
        {
            "confidence_threshold": 0.7,
            "device": "auto",
            "model_dir": tmp_path,
        }
    ]
    assert progress == [
        "Initializing ONNX OCR engine...",
        "OCR engine ready on DirectML GPU",
    ]
