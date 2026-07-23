# 监控客户端打包与分发

> 兼容方案：本文记录旧 PaddleOCR GPU 完整包。当前生产监控端使用体积更小的
> ONNX Runtime DirectML 包，构建与验证方式见
> [`docs/onnx-ocr-validation.md`](onnx-ocr-validation.md)。

本文说明如何在 Windows 上构建包含 PaddleOCR GPU 运行环境和 OCR 模型的监控客户端压缩包。
该发行包只负责 EVE 窗口截图、OCR 名单识别、OCR snapshot 和 heartbeat 上报，不包含
独立频道客户端或预警客户端。

“离线模型”表示目标机器首次识别时不需要下载 OCR 模型。监控客户端仍需连接 EVE Sentry
服务端才能上报名单和在线状态。

## 发行包内容

构建产物目录为:

```text
dist/EVE-Sentry-Monitor-Bundled/
├── EVE-Sentry-Monitor.exe
└── _internal/
    ├── models/
    │   ├── PP-OCRv6_medium_det/
    │   │   ├── inference.json
    │   │   ├── inference.pdiparams
    │   │   └── inference.yml
    │   └── PP-OCRv6_medium_rec/
    │       ├── inference.json
    │       ├── inference.pdiparams
    │       └── inference.yml
    ├── paddle/
    ├── paddleocr/
    └── PyQt6/
```

客户端启动 OCR 时会优先使用 `_internal/models` 内的检测和识别模型。只有包内模型不完整
或运行源码版本时，PaddleOCR 才会继续使用用户缓存或按其默认行为下载模型。

## 构建环境

- Windows 10/11 x64。
- Python 虚拟环境已安装项目依赖。
- `paddleocr==3.7.0`。
- `paddlepaddle-gpu==3.2.0` CUDA 11.8 版本。
- `PyInstaller`。
- 本机模型缓存中存在 `PP-OCRv6_medium_det` 和 `PP-OCRv6_medium_rec`。

安装 PyInstaller 并预下载模型:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe setup_models.py
```

默认从以下目录收集模型:

```text
%USERPROFILE%\.paddlex\official_models\PP-OCRv6_medium_det
%USERPROFILE%\.paddlex\official_models\PP-OCRv6_medium_rec
```

模型放在其他位置时，在构建前指定缓存根目录。该目录下仍需保留上述两个模型子目录:

```powershell
$env:EVE_SENTRY_OCR_MODEL_CACHE = "D:\PaddleModels"
```

## 构建和压缩

在仓库根目录执行:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  packaging\eve-sentry-monitor-bundled.spec
```

构建配置会在模型文件缺失时直接失败，避免生成一个首次运行仍需联网下载模型的压缩包。

构建成功后压缩完整目录。不要只分发 EXE，OCR、Qt、Paddle 和模型都位于
`_internal` 目录:

```powershell
Compress-Archive `
  -LiteralPath .\dist\EVE-Sentry-Monitor-Bundled `
  -DestinationPath .\dist\EVE-Sentry-Monitor-GPU-With-Models.zip `
  -CompressionLevel Optimal
```

2026-07-22 参考构建的模型文件约 132.67 MB，完整目录约 1.49 GB，ZIP 约 826 MB。
具体体积会随 Paddle、CUDA、PyQt6 和 PyInstaller 版本变化。

## 分发和启动

目标机器无需安装 Python、PaddleOCR 或单独下载模型，但需要:

- 兼容的 NVIDIA GPU 和驱动。
- 能访问配置的 EVE Sentry 服务端。
- EVE 窗口处于可截图状态；游戏最小化后 Windows 通常无法提供有效窗口画面。

解压整个 ZIP 后启动:

```powershell
$env:EVE_SENTRY_INTEL_URL = "http://YOUR_SERVER"
.\EVE-Sentry-Monitor-Bundled\EVE-Sentry-Monitor.exe
```

需要启动后自动开始监控时:

```powershell
$env:EVE_SENTRY_INTEL_URL = "http://YOUR_SERVER"
$env:EVE_SENTRY_AUTO_START_MONITOR = "1"
.\EVE-Sentry-Monitor-Bundled\EVE-Sentry-Monitor.exe
```

GPU 环境临时不可用时可以强制使用 CPU，但 GPU 发行包体积不会因此变小:

```powershell
$env:EVE_SENTRY_OCR_DEVICE = "cpu"
.\EVE-Sentry-Monitor-Bundled\EVE-Sentry-Monitor.exe
```

## 验收

构建后先检查 EXE 和两个模型参数文件:

```powershell
$root = ".\dist\EVE-Sentry-Monitor-Bundled"
Test-Path "$root\EVE-Sentry-Monitor.exe"
Test-Path "$root\_internal\models\PP-OCRv6_medium_det\inference.pdiparams"
Test-Path "$root\_internal\models\PP-OCRv6_medium_rec\inference.pdiparams"
```

三个结果都应为 `True`。分发前记录压缩包校验值:

```powershell
Get-FileHash .\dist\EVE-Sentry-Monitor-GPU-With-Models.zip -Algorithm SHA256
```

目标机器首次启动后，确认:

- 客户端界面可以正常打开。
- OCR 状态显示使用 GPU，且不会出现模型下载进度。
- 开始监控后可以识别 EVE 本地成员列表。
- 服务端 `GET /api/v1/clients` 能看到 `detector_client` heartbeat。
- 服务端 `GET /api/v1/active-intel?source=eve-sentry-detector` 能看到当前 OCR 名单。

如果首次识别仍尝试下载模型，优先检查是否只复制了 EXE、模型目录是否完整，以及运行的
是否为 `EVE-Sentry-Monitor-Bundled` 新构建版本。
