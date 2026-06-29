"""Pre-download PaddleOCR models so the main app starts instantly.

Run once: ``uv run python setup_models.py``
"""

import os

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
os.environ.setdefault("EVE_SENTRY_OCR_DEVICE", "auto")

print(">>> Downloading PaddleOCR models (one-time, ~100 MB)...")
print(">>> This may take 1-2 minutes. Please wait.\n")

import paddle
from paddleocr import PaddleOCR

device = "gpu:0" if paddle.device.is_compiled_with_cuda() else "cpu"
print(f">>> Paddle runtime device: {device}")
print("Initializing PaddleOCR (this triggers model downloads)...")
ocr = PaddleOCR(
    lang="en",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
    device=device,
)

# Run a dummy recognition to ensure all models are loaded
import numpy as np
img = np.zeros((30, 100, 3), dtype="uint8")
ocr.predict(img)

print("\n>>> Done! All models cached. The main app will start instantly now.")
