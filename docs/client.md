# 监控客户端

## 运行要求

- Windows 10/11，Python 3.11+ 或已打包客户端。
- EVE Online 使用窗口化或无边框窗口模式；游戏最小化时 Windows 通常无法提供有效画面。
- DirectML 发行版不要求 CUDA，支持大多数现代 AMD、Intel 和 NVIDIA 显卡。
- 模型目录包含 PP-OCRv6 medium detector 和 recognizer 的 `model.onnx`。

源码运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-onnx.txt
$env:EVE_SENTRY_OCR_BACKEND = "onnx"
$env:EVE_SENTRY_OCR_DEVICE = "dml"
.\scripts\start_monitor_client.ps1 -Server http://127.0.0.1:8765
```

也可直接执行 `python -m app.detector_client`。

## 使用流程

1. 填写服务端地址和网页账号中创建的桌面设备密钥。
2. 客户端会自动定位当前活跃的 EVE Chatlogs 目录；环境变量可用于强制指定路径。
3. 点击刷新或等待客户端自动刷新窗口列表。
4. 在下拉框选择一个 EVE 窗口，必要时重新框选成员列表区域。
5. 开启监控以启动截图、OCR、快照上报和心跳。
6. 开启预警以显示 SSE 浮窗和告警声音。

监控和预警是两个独立开关。停止监控会停止本机采集并让该节点从其他预警客户端移除；
关闭预警只停止本机订阅和浮窗，不影响 OCR 上报。

浮窗每个方框代表一个正在监控的星系节点：安全为绿色，来敌为红色并显示实时人数。
本机检测到红色敌对图标时会立即更新当前星系；服务端推送继续用于同步其他节点。
客户端不发送右下角系统通知，预警信息统一显示在可缩放的顶部浮窗中。

## EVE 身份校验

设备密钥和身份状态使用 Windows DPAPI 保存。新密钥或本地索引缺失时会扫描全部历史
聊天日志中的 `Listener`；完成首次验证后只处理新增日志文件。新增文件暂未写出完整
`Listener` 时保持待处理，下一个 10 秒周期继续检查。

客户端启动后会在后台预校验密钥；同一运行周期内启动监控或预警时复用校验结果，
不再重复等待网络请求。密钥、服务端地址变化或服务端拒绝认证时会立即清除结果并重验。

OCR 推理运行时也会在界面启动后后台加载，选中窗口开始监控时复用已加载的首个引擎。
预警开启时直接建立 SSE 并请求 Bootstrap，运行心跳不再阻塞首次连接。

认证或 ESI 暂时失败时，客户端不会阻塞界面，但监控、预警和上报保持暂停。确认角色
不属于允许军团且不在用户白名单时，服务端会禁用用户，客户端必须更换新签发密钥。

## 主要配置

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `EVE_SENTRY_INTEL_URL` | `http://114.132.167.239:8765` | 服务端统一入口 |
| `EVE_SENTRY_CHATLOG_DIR` | EVE 默认 Chatlogs | 本地聊天日志目录 |
| `EVE_SENTRY_SCAN_INTERVAL` | `2` | OCR 扫描间隔，界面范围 1-10 秒 |
| `EVE_SENTRY_WINDOW_KEYWORD` | `EVE -` | 窗口标题过滤关键字 |
| `EVE_SENTRY_HEARTBEAT_INTERVAL` | `15` | 心跳间隔，最小 5 秒 |
| `EVE_SENTRY_INTEL_TIMEOUT` | `10` | HTTP 请求超时秒数 |
| `EVE_SENTRY_OCR_BACKEND` | 源码默认 `paddle` | 生产客户端设置为 `onnx` |
| `EVE_SENTRY_ONNX_MODEL_DIR` | 自动搜索 | ONNX 模型根目录 |
| `EVE_SENTRY_OCR_DEVICE` | `auto` | `dml`、`cuda`、`cpu` 或 `auto` |
| `EVE_SENTRY_AUTO_START_MONITOR` | `0` | 启动后自动请求开启监控 |
| `EVE_SENTRY_PUBLISH_INTEL` | `1` | 设置为 `0` 可进行不上报的本地测试 |

## 打包

当前轻量发行包使用 `packaging/eve-sentry-monitor-onnx.spec`：

```powershell
.\.venv\Scripts\python -m pip install pyinstaller
$env:EVE_SENTRY_ONNX_MODEL_CACHE = "$PWD\.runtime\onnx-models"
.\.venv\Scripts\python -m PyInstaller --clean --noconfirm packaging\eve-sentry-monitor-onnx.spec
Compress-Archive -Path .\dist\EVE-Sentry-Monitor-ONNX -DestinationPath .\dist\EVE-Sentry-Monitor-ONNX.zip -Force
```

输出目录必须整体分发，不能只复制 EXE。验收文件：

```powershell
$root = ".\dist\EVE-Sentry-Monitor-ONNX"
Test-Path "$root\EVE-Sentry-Monitor.exe"
Test-Path "$root\_internal\models\PP-OCRv6_medium_det\model.onnx"
Test-Path "$root\_internal\models\PP-OCRv6_medium_rec\model.onnx"
Get-FileHash .\dist\EVE-Sentry-Monitor-ONNX.zip -Algorithm SHA256
```

`packaging/eve-sentry-monitor-bundled.spec` 是体积较大的 Paddle 兼容构建，不是当前推荐发行版。

## 排查

- `服务连接异常`：先访问 `GET /api/health`，再确认地址、设备密钥和服务端认证模式。
- `OCR snapshot upload failed`：检查反向代理、请求超时和服务端日志；客户端只对传输错误重试一次。
- 窗口列表缺少新窗口：等待自动刷新或点击刷新，确认窗口标题包含配置关键字。
- 识别区域偏移：重新框选成员列表；客户端会按窗口位置、大小和 DPI 缩放保存区域。
- GPU 回退：检查日志中的 detector/recognizer provider，DirectML 应为 `DmlExecutionProvider`。
- 当前星系错误：确认窗口标题角色名与对应 Local 日志的 `Listener` 一致。

界面烟雾测试不会连接服务端或执行真实 OCR：

```powershell
python scripts/monitor_ui_smoke.py --json --screenshot .\monitor-ui-smoke.png
```
