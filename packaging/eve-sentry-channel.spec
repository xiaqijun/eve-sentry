# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller build for the lightweight EVE intel channel client."""

from pathlib import Path


ROOT = Path(SPECPATH).parent

hiddenimports = ["win32timezone", "PyQt6.sip"]
datas = [
    (str(ROOT / "resources" / "spin-up.svg"), "resources"),
    (str(ROOT / "resources" / "spin-down.svg"), "resources"),
]
excludes = [
    "cv2", "numpy", "onnxruntime", "paddleocr", "paddlepaddle",
    "rapidocr", "torch", "torchvision", "scipy", "pandas",
    "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.Qt3DInput",
    "PyQt6.Qt3DLogic", "PyQt6.Qt3DExtras", "PyQt6.Qt3DAnimation",
    "PyQt6.QtPdf", "PyQt6.QtPdfWidgets",
]

a = Analysis(
    [str(ROOT / "app" / "channel_client_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EVE-Sentry-Channel",
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
    name="EVE-Sentry-Channel",
)
