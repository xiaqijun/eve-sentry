# ONNX Runtime GPU OCR validation

The lightweight monitor build keeps PaddleOCR as the default source-build backend
and adds an opt-in `onnx` backend. On Windows the ONNX build uses DirectML, so it
can use the NVIDIA GPU without bundling Paddle's roughly 796 MB `phi.dll` or a
separate CUDA/cuDNN runtime.

## Reference result

The validation image is `.runtime/paddle-gpu-benchmark_capture.png` (179 x 762).
Both backends recognized the same ten lines, including all nine character names.

| Backend | Initialization | First inference | Warm inference |
| --- | ---: | ---: | ---: |
| PaddleOCR CUDA 11.8 | about 23.8 s | included in initialization | 110-112 ms |
| ONNX Runtime DirectML | about 2.5 s | about 0.92 s | 129-173 ms, 150 ms average |

Through the final `OCREngine` wrapper, initialization plus the first source-tree
recognition took about 3.33 s. A fresh PyInstaller probe process took about 7.75 s
for initialization plus its first recognition; it then returned the same ten lines
while reporting `DirectML GPU` as the active provider.

The converted PP-OCRv6 medium models are about 132 MB total. The DirectML wheel is
about 23 MB, compared with the 796 MB uncompressed Paddle `phi.dll` alone.

The 2026-07-23 PyInstaller validation build was 411.64 MB unpacked and 209.89 MB
as an Optimal `Compress-Archive` ZIP. It contained no `phi.dll`, `paddle`, or
`paddleocr` files. The equivalent Paddle package was about 1.49 GB unpacked and
826 MB compressed.

## Convert the models

Paddle2ONNX 2.1 currently requires a compatible Python 3.12 conversion environment.
The verified converter versions are Paddle 3.1.1, Paddle2ONNX 2.1.0 and ONNX 1.17.

```powershell
python -m venv .runtime\onnx-converter
.\.runtime\onnx-converter\Scripts\python.exe -m pip install `
  paddlepaddle==3.1.1 paddle2onnx==2.1.0 onnx==1.17.0 PyYAML
$env:PATH = "$PWD\.runtime\onnx-converter\Scripts;$env:PATH"
.\.runtime\onnx-converter\Scripts\python.exe `
  scripts\convert_ocr_models_to_onnx.py
```

The converter embeds the recognition character dictionary in ONNX metadata. Do
not distribute a recognition model that was converted without this final step.

## Run and benchmark

Install the lightweight dependencies in a clean environment:

```powershell
python -m venv .venv-onnx
.\.venv-onnx\Scripts\python.exe -m pip install -r requirements-onnx.txt
```

Select the backend explicitly for a source run:

```powershell
$env:EVE_SENTRY_OCR_BACKEND = "onnx"
$env:EVE_SENTRY_ONNX_MODEL_DIR = "$PWD\.runtime\onnx-models"
python main.py
```

The live probe accepts `--engine paddle` or `--engine onnx`:

```powershell
.\.venv-onnx\Scripts\python.exe scripts\live_ocr_probe.py `
  --engine onnx --frames 8 --out .runtime\onnx-directml-benchmark
```

`EVE_SENTRY_OCR_DEVICE=cpu` forces CPU. `auto` prefers DirectML on Windows and
falls back to CUDA or CPU when another provider is the only available runtime.

## Build the lightweight client

The ONNX spec excludes Paddle and PaddleOCR, includes the two converted ONNX
models, and selects the `onnx` backend through a PyInstaller runtime hook.

```powershell
.\.venv-onnx\Scripts\python.exe -m pip install pyinstaller
$env:EVE_SENTRY_ONNX_MODEL_CACHE = "$PWD\.runtime\onnx-models"
.\.venv-onnx\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  packaging\eve-sentry-monitor-onnx.spec
```

The output directory is `dist/EVE-Sentry-Monitor-ONNX`. Distribute the complete
directory rather than only the executable, because the models and DirectML runtime
are stored under `_internal`.
