"""OCR wrapper using PaddleOCR."""

import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


class OCREngine:
    """Wrap PaddleOCR for local text recognition on screen captures."""

    def __init__(
        self,
        lang: str = "en",
        confidence_threshold: float = 0.7,
        device: str | None = None,
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._ocr: Optional[object] = None
        self._lang = lang
        self._device = device or os.environ.get("EVE_SENTRY_OCR_DEVICE", "auto")

    def _init_ocr(self, progress: Callable[[str], None] | None = None) -> None:
        """Lazy-init the PaddleOCR instance."""

        def _report(message: str) -> None:
            logger.info(message)
            if progress:
                progress(message)

        try:
            import paddle
            from paddleocr import PaddleOCR

            runtime = self._resolve_runtime(paddle)
            _report(
                "Initializing OCR engine "
                f"(lang={self._lang}, device={runtime['device_label']})..."
            )
            self._ocr = self._create_paddle_ocr(
                PaddleOCR,
                device_arg=str(runtime["device_arg"]),
                use_gpu=bool(runtime["use_gpu"]),
            )
            _report(f"OCR engine ready on {runtime['device_label']}")
            logger.info(
                "PaddleOCR initialised (lang=%s, device=%s, cuda_build=%s)",
                self._lang,
                runtime["device_label"],
                runtime["cuda_build"],
            )
        except Exception:
            logger.exception("Failed to initialise PaddleOCR")
            _report("OCR engine initialization failed")
            self._ocr = None

    def _resolve_runtime(self, paddle) -> dict[str, object]:
        """Choose CPU or GPU runtime from local support and env override."""
        compiled_with_cuda = bool(paddle.device.is_compiled_with_cuda())
        requested = (self._device or "auto").strip().lower()

        if requested == "cpu":
            return {
                "device_arg": "cpu",
                "device_label": "CPU",
                "use_gpu": False,
                "cuda_build": compiled_with_cuda,
            }

        if requested not in {"auto", "gpu", "gpu:0"}:
            logger.warning(
                "Unknown OCR device override '%s'; falling back to auto",
                requested,
            )
            requested = "auto"

        if compiled_with_cuda:
            return {
                "device_arg": "gpu:0",
                "device_label": "GPU 0",
                "use_gpu": True,
                "cuda_build": True,
            }

        if requested.startswith("gpu"):
            logger.warning(
                "GPU OCR was requested but the installed Paddle build has no CUDA "
                "support; falling back to CPU"
            )

        return {
            "device_arg": "cpu",
            "device_label": "CPU",
            "use_gpu": False,
            "cuda_build": False,
        }

    def _create_paddle_ocr(
        self,
        factory: Callable[..., object],
        device_arg: str,
        use_gpu: bool,
    ) -> object:
        """Create PaddleOCR across 2.x/3.x constructor option changes."""
        modern_options = {
            "lang": self._lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": device_arg,
        }
        modern_options.update(self._bundled_model_options())
        try:
            return factory(**modern_options)
        except TypeError:
            return factory(
                lang=self._lang,
                use_angle_cls=False,
                use_gpu=use_gpu,
            )

    def _bundled_model_options(self) -> dict[str, str]:
        """Return packaged PaddleOCR model paths when both models are present."""
        bundle_path = getattr(sys, "_MEIPASS", None)
        if not bundle_path:
            return {}

        bundle_root = Path(bundle_path)
        model_root = bundle_root / "models"
        detection_dir = model_root / "PP-OCRv6_medium_det"
        recognition_dir = model_root / "PP-OCRv6_medium_rec"
        required_file = "inference.pdiparams"
        if not (detection_dir / required_file).is_file() or not (
            recognition_dir / required_file
        ).is_file():
            return {}

        return {
            "text_detection_model_dir": str(detection_dir),
            "text_recognition_model_dir": str(recognition_dir),
        }

    def recognize(
        self,
        image: Image.Image,
        progress: Callable[[str], None] | None = None,
    ) -> list[tuple[str, float]]:
        """Run OCR on *image* and return high-confidence text lines."""
        if self._ocr is None:
            self._init_ocr(progress=progress)
        if self._ocr is None:
            return []

        prepared = self._prepare_image(image)
        img_array = np.array(prepared)

        try:
            raw = self._run_ocr(img_array)
        except Exception:
            logger.exception("OCR recognition failed")
            return []

        return self._parse_results(raw)

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        """Apply lightweight enhancement before OCR."""
        grayscale = image.convert("L")
        grayscale = ImageOps.autocontrast(grayscale)
        return grayscale.convert("RGB")

    def _run_ocr(self, img_array: np.ndarray):
        """Run OCR using the available PaddleOCR API."""
        if hasattr(self._ocr, "predict"):
            return self._ocr.predict(img_array)
        if hasattr(self._ocr, "ocr"):
            return self._ocr.ocr(img_array, cls=False)
        raise RuntimeError("PaddleOCR object exposes neither predict() nor ocr()")

    def _parse_results(self, raw) -> list[tuple[str, float]]:
        """Parse PaddleOCR 3.x ``predict`` and 2.x ``ocr`` result shapes."""
        if raw is None:
            return []

        results: list[tuple[str, float]] = []

        if isinstance(raw, dict):
            raw = [raw]

        for page in raw:
            if page is None:
                continue
            if isinstance(page, dict):
                self._extend_from_dict_page(page, results)
            elif isinstance(page, (list, tuple)):
                for block in page:
                    self._append_legacy_block(block, results)

        return results

    def _extend_from_dict_page(
        self,
        page: dict,
        results: list[tuple[str, float]],
    ) -> None:
        """Append PaddleOCR 3.x dict-style recognition results."""
        texts = page.get("rec_texts") or []
        scores = page.get("rec_scores") or []
        for text, conf in zip(texts, scores):
            self._append_text(text, conf, results)

    def _append_legacy_block(
        self,
        block,
        results: list[tuple[str, float]],
    ) -> None:
        """Append one PaddleOCR 2.x block: ``[bbox, (text, confidence)]``."""
        if not isinstance(block, (list, tuple)) or len(block) < 2:
            return
        info = block[1]
        if not isinstance(info, (list, tuple)) or len(info) < 2:
            return
        self._append_text(info[0], info[1], results)

    def _append_text(
        self,
        text,
        confidence,
        results: list[tuple[str, float]],
    ) -> None:
        """Append a normalized OCR line if it passes the confidence threshold."""
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            return
        text = str(text).strip()
        if text and conf >= self._confidence_threshold:
            results.append((text, conf))
