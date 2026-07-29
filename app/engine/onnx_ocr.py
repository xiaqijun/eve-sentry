"""ONNX Runtime OCR backend using RapidOCR and Windows DirectML."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAMES = ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
MODEL_FILENAME = "model.onnx"


def _candidate_model_roots(model_dir: str | Path | None) -> Iterable[Path]:
    """Yield configured, bundled, and development ONNX model roots."""
    if model_dir:
        yield Path(model_dir)
        return

    env_dir = os.environ.get("EVE_SENTRY_ONNX_MODEL_DIR")
    if env_dir:
        yield Path(env_dir)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        yield Path(local_app_data) / "EVE Sentry" / "models"

    bundle_path = getattr(sys, "_MEIPASS", None)
    if bundle_path:
        yield Path(bundle_path) / "models"

    yield Path.cwd() / "models"
    yield Path(__file__).resolve().parents[2] / ".runtime" / "onnx-models"


def resolve_onnx_model_paths(
    model_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Locate the converted detector and recognizer ONNX model files."""
    checked: list[Path] = []
    for root in _candidate_model_roots(model_dir):
        root = root.expanduser().resolve()
        if root in checked:
            continue
        checked.append(root)
        paths = tuple(root / name / MODEL_FILENAME for name in MODEL_NAMES)
        if all(path.is_file() for path in paths):
            return paths[0], paths[1]

    locations = ", ".join(str(path) for path in checked)
    raise FileNotFoundError(
        "PP-OCRv6 ONNX models were not found. "
        "Set EVE_SENTRY_ONNX_MODEL_DIR or place both model folders under "
        f"a bundled models directory. Checked: {locations}"
    )


def select_onnx_provider(
    providers: Iterable[str],
    device: str | None,
) -> tuple[bool, bool, str]:
    """Return RapidOCR CUDA/DirectML flags and the expected provider label."""
    available = set(providers)
    requested = (device or "auto").strip().lower()
    if requested == "cpu":
        return False, False, "CPU"

    if requested not in {"auto", "gpu", "gpu:0", "cuda", "dml", "directml"}:
        logger.warning(
            "Unknown ONNX OCR device override '%s'; falling back to auto",
            requested,
        )
        requested = "auto"

    prefer_cuda = requested == "cuda"
    prefer_dml = requested in {"dml", "directml"}
    if prefer_cuda and "CUDAExecutionProvider" in available:
        return True, False, "CUDA GPU 0"
    if prefer_dml and "DmlExecutionProvider" in available:
        return False, True, "DirectML GPU"

    if "DmlExecutionProvider" in available:
        return False, True, "DirectML GPU"
    if "CUDAExecutionProvider" in available:
        return True, False, "CUDA GPU 0"

    if requested != "auto":
        logger.warning(
            "GPU OCR was requested but no ONNX GPU provider is available; "
            "falling back to CPU"
        )
    return False, False, "CPU"


class RapidOCROnnxAdapter:
    """Expose RapidOCR results through the PaddleOCR ``predict`` shape."""

    def __init__(
        self,
        *,
        confidence_threshold: float,
        device: str | None = None,
        model_dir: str | Path | None = None,
    ) -> None:
        try:
            import onnxruntime as ort
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "ONNX OCR requires rapidocr and an ONNX Runtime package"
            ) from exc

        det_model, rec_model = resolve_onnx_model_paths(model_dir)
        use_cuda, use_dml, provider_label = select_onnx_provider(
            ort.get_available_providers(),
            device,
        )
        params = {
            "Global.use_cls": False,
            "Global.text_score": confidence_threshold,
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.use_cuda": use_cuda,
            "EngineConfig.onnxruntime.use_dml": use_dml,
            "Det.model_path": str(det_model),
            "Det.limit_type": "max",
            "Det.mean": [0.485, 0.456, 0.406],
            "Det.std": [0.229, 0.224, 0.225],
            "Det.thresh": 0.2,
            "Det.box_thresh": 0.45,
            "Det.unclip_ratio": 1.4,
            "Det.use_dilation": False,
            "Rec.model_path": str(rec_model),
        }
        self._ocr = RapidOCR(params=params)
        self.provider_label = provider_label
        self._verify_provider(provider_label)

    def _verify_provider(self, provider_label: str) -> None:
        """Warn when ONNX Runtime silently falls back from GPU to CPU."""
        expected = None
        if provider_label.startswith("DirectML"):
            expected = "DmlExecutionProvider"
        elif provider_label.startswith("CUDA"):
            expected = "CUDAExecutionProvider"
        if expected is None:
            return

        detector_providers = self._ocr.text_det.session.session.get_providers()
        recognizer_providers = self._ocr.text_rec.session.session.get_providers()
        if not detector_providers or not recognizer_providers:
            return
        if detector_providers[0] != expected or recognizer_providers[0] != expected:
            logger.warning(
                "ONNX OCR requested %s but sessions use detector=%s recognizer=%s",
                expected,
                detector_providers[0],
                recognizer_providers[0],
            )
            self.provider_label = "CPU fallback"

    def predict(self, img_array: np.ndarray) -> list[dict[str, list]]:
        """Run OCR and return a PaddleOCR 3.x-compatible page dictionary."""
        output = self._ocr(img_array, use_cls=False)
        texts = list(getattr(output, "txts", None) or [])
        scores = [float(score) for score in (getattr(output, "scores", None) or [])]
        return [{"rec_texts": texts, "rec_scores": scores}]
