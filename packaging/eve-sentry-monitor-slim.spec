# -*- mode: python ; coding: utf-8 -*-
r"""Slim PyInstaller build for the local monitor client.

Build from the repository root with:
    .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\eve-sentry-monitor-slim.spec

For the smallest package, build from a CPU-only virtualenv that installs
``paddlepaddle`` instead of ``paddlepaddle-gpu``. A GPU Paddle environment
will still bundle CUDA/MKL Paddle DLLs and produce a much larger directory.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


ROOT = Path(SPECPATH).parent


def keep_paddleocr_module(name: str) -> bool:
    excluded_prefixes = (
        "paddleocr.__main__",
        "paddleocr._cli",
        "paddleocr._doc2md",
    )
    return not name.startswith(excluded_prefixes)


def copy_first_metadata(*package_names: str):
    for package_name in package_names:
        try:
            return copy_metadata(package_name)
        except Exception:
            continue
    return []


hiddenimports = [
    "win32timezone",
    "cv2",
    "numpy",
    "pyclipper",
    "shapely",
]
hiddenimports += collect_submodules("paddleocr", filter=keep_paddleocr_module)

datas = [
    (str(ROOT / "resources" / "alert.wav"), "resources"),
    (str(ROOT / "resources" / "spin-up.svg"), "resources"),
    (str(ROOT / "resources" / "spin-down.svg"), "resources"),
]
datas += collect_data_files(
    "paddleocr",
    excludes=[
        "**/__pycache__/**",
        "**/_doc2md/**",
        "**/tests/**",
    ],
)
datas += copy_metadata("paddleocr")
datas += copy_first_metadata("paddlepaddle", "paddlepaddle-gpu")
datas += copy_metadata("pypdfium2")

binaries = []
binaries += collect_dynamic_libs("paddle")
binaries += collect_dynamic_libs("cv2")
binaries += collect_dynamic_libs("shapely")

excludes = [
    "IPython",
    "jupyter",
    "matplotlib",
    "notebook",
    "pandas",
    "pytest",
    "scipy",
    "tensorflow",
    "torch",
    "torchvision",
    "hf_xet",
    "huggingface_hub.inference",
    "PIL.AvifImagePlugin",
    "PIL._avif",
    "pypdfium2_raw",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "paddle.distributed",
    "paddle.incubate.distributed",
    "paddle.tensorrt",
    "paddle.audio",
    "paddle.dataset",
    "paddle.text",
    "paddle.vision",
    "paddleocr._doc2md",
    "shapely.tests",
    "modelscope.models",
    "modelscope.msdatasets",
    "modelscope.pipelines",
    "modelscope.preprocessors",
    "modelscope.trainers",
    "modelscope.exporters",
    "modelscope.server",
    "modelscope.tools",
    "modelscope.outputs",
]

a = Analysis(
    [str(ROOT / "app" / "detector_client.py")],
    pathex=[str(ROOT / "packaging" / "stubs"), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# The desktop monitor only passes in-memory screenshots to OCR.  PyInstaller's
# generic hooks otherwise add video decoding, PDF/AVIF support, an optional
# HuggingFace Xet accelerator, and every Qt translation catalog.
unused_binary_fragments = (
    "opencv_videoio_ffmpeg",
    "pypdfium2_raw\\pdfium.dll",
    "hf_xet\\hf_xet",
    "pil\\_avif",
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
    name="eve-sentry-monitor-slim",
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
    name="eve-sentry-monitor-slim",
)
