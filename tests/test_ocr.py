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
