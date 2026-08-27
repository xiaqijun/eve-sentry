from types import SimpleNamespace

import numpy as np
import pytest

from app.engine.onnx_ocr import (
    MODEL_FILENAME,
    MODEL_NAMES,
    RapidOCROnnxAdapter,
    resolve_onnx_model_paths,
    select_onnx_provider,
)


def test_resolve_onnx_model_paths_from_explicit_root(tmp_path):
    expected = []
    for model_name in MODEL_NAMES:
        model_path = tmp_path / model_name / MODEL_FILENAME
        model_path.parent.mkdir()
        model_path.write_bytes(b"onnx")
        expected.append(model_path)

    assert resolve_onnx_model_paths(tmp_path) == tuple(expected)


def test_resolve_onnx_model_paths_reports_missing_models(tmp_path):
    with pytest.raises(FileNotFoundError, match="PP-OCRv6 ONNX models"):
        resolve_onnx_model_paths(tmp_path)


@pytest.mark.parametrize(
    ("providers", "device", "expected"),
    [
        (["DmlExecutionProvider", "CPUExecutionProvider"], "auto", (False, True, "DirectML GPU")),
        (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda", (True, False, "CUDA GPU 0")),
        (["DmlExecutionProvider", "CPUExecutionProvider"], "cpu", (False, False, "CPU")),
        (["CPUExecutionProvider"], "gpu", (False, False, "CPU")),
    ],
)
def test_select_onnx_provider(providers, device, expected):
    assert select_onnx_provider(providers, device) == expected


def test_adapter_predict_returns_paddle_compatible_page():
    adapter = RapidOCROnnxAdapter.__new__(RapidOCROnnxAdapter)
    adapter._ocr = lambda _image, use_cls: SimpleNamespace(
        txts=("Alice", "Bob"),
        scores=(0.98, np.float32(0.87)),
        boxes=np.asarray(
            [
                [[1, 2], [10, 2], [10, 12], [1, 12]],
                [[2, 20], [12, 20], [12, 30], [2, 30]],
            ],
            dtype=np.float32,
        ),
    )

    result = adapter.predict(np.zeros((4, 4, 3), dtype=np.uint8))

    assert result == [
        {
            "rec_texts": ["Alice", "Bob"],
            "rec_scores": [0.98, pytest.approx(0.87)],
            "rec_boxes": [
                [[1.0, 2.0], [10.0, 2.0], [10.0, 12.0], [1.0, 12.0]],
                [[2.0, 20.0], [12.0, 20.0], [12.0, 30.0], [2.0, 30.0]],
            ],
        }
    ]
