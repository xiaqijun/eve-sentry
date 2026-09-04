# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller build for the lightweight DirectML ONNX monitor client."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


ROOT = Path(SPECPATH).parent
MODEL_CACHE = Path(
    os.environ.get(
        "EVE_SENTRY_ONNX_MODEL_CACHE",
        ROOT / ".runtime" / "onnx-models",
    )
)
MODEL_NAMES = ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
MODEL_FILENAME = "model.onnx"


def keep_rapidocr_module(name: str) -> bool:
    excluded_prefixes = (
        "rapidocr.inference_engine.mnn",
        "rapidocr.inference_engine.openvino",
        "rapidocr.inference_engine.paddle",
        "rapidocr.inference_engine.pytorch",
        "rapidocr.inference_engine.tensorrt",
    )
    return not name.startswith(excluded_prefixes)


def collect_model_files():
    files = []
    for model_name in MODEL_NAMES:
        model_path = MODEL_CACHE / model_name / MODEL_FILENAME
        if not model_path.is_file():
            raise FileNotFoundError(f"OCR model is missing: {model_path}")
        files.append((str(model_path), f"models/{model_name}"))
    return files


hiddenimports = [
    "antlr4",
    "win32timezone",
    "cv2",
    "numpy",
    "omegaconf.grammar.gen.OmegaConfGrammarLexer",
    "omegaconf.grammar.gen.OmegaConfGrammarParser",
    "omegaconf.grammar.gen.OmegaConfGrammarParserListener",
    "omegaconf.grammar.gen.OmegaConfGrammarParserVisitor",
    "pyclipper",
    "shapely",
]
hiddenimports += collect_submodules("rapidocr", filter=keep_rapidocr_module)

datas = [
    (str(ROOT / "resources" / "alert.wav"), "resources"),
    (str(ROOT / "resources" / "spin-up.svg"), "resources"),
    (str(ROOT / "resources" / "spin-down.svg"), "resources"),
    (str(ROOT / "resources" / "update_public_key.pem"), "resources"),
]
datas += collect_model_files()
datas += collect_data_files(
    "rapidocr",
    excludes=[
        "**/__pycache__/**",
        "**/inference_engine/pytorch/**",
        "**/models/PP-OCRv6_det_small.onnx",
        "**/models/PP-OCRv6_rec_small.onnx",
    ],
)
datas += copy_metadata("rapidocr")
datas += copy_metadata("onnxruntime-directml")

binaries = []
binaries += collect_dynamic_libs("onnxruntime")
binaries += collect_dynamic_libs("cv2")
binaries += collect_dynamic_libs("shapely")

excludes = [
    "IPython",
    "jupyter",
    "matplotlib",
    "notebook",
    "paddle",
    "paddleocr",
    "pandas",
    "pytest",
    "scipy",
    "tensorflow",
    "torch",
    "torchvision",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "rapidocr.inference_engine.mnn",
    "rapidocr.inference_engine.openvino",
    "rapidocr.inference_engine.paddle",
    "rapidocr.inference_engine.pytorch",
    "rapidocr.inference_engine.tensorrt",
]

a = Analysis(
    [str(ROOT / "app" / "detector_client.py")],
    pathex=[str(ROOT / "packaging" / "stubs"), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "runtime_hooks" / "onnx_backend.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

unused_binary_fragments = (
    "opencv_videoio_ffmpeg",
    "pyqt6\\qtpdf",
    "pyqt6\\qt6\\bin\\qt6pdf.dll",
)
a.binaries = [
    entry
    for entry in a.binaries
    if not any(fragment in entry[0].lower() for fragment in unused_binary_fragments)
]
a.datas = [
    entry
    for entry in a.datas
    if not entry[0].lower().startswith("pyqt6\\qt6\\translations\\")
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EVE-Sentry-Monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EVE-Sentry-Monitor-ONNX",
)
