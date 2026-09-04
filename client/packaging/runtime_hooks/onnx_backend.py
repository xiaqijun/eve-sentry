"""Select the lightweight ONNX OCR backend in the ONNX client build."""

import os

os.environ.setdefault("EVE_SENTRY_OCR_BACKEND", "onnx")
os.environ.setdefault("EVE_SENTRY_OCR_DEVICE", "auto")
