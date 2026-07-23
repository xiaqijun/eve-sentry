# EVE Sentry 文档

EVE Sentry 是面向 EVE Online 本地成员列表的实时情报系统。Windows 客户端负责窗口截图、红色声望图标检测、OCR 人名识别和现场预警；服务端负责情报持久化、角色校验、敌我分类、事件分发、态势图、来袭报表和 QQ 机器人推送。

## 当前实现

- 监控与预警已经集成到同一个 Windows 客户端，通过“开始监控”和“开启预警”两个独立开关控制。
- 监控只处理下拉列表中选中的 EVE 窗口，并从对应角色的本地 Chatlogs 获取当前星系。
- 成员列表先检测红色声望图标，再识别对应人名；连续两帧无法可靠定位时回退上传完整 OCR 名单。
- 本机检测到敌对后立即更新浮窗；服务器收到上报后向所有已开启预警的客户端推送实时人数。
- 浮窗为每个在线监控星系显示一个状态方框：安全为绿色，存在敌对为红色，并实时显示敌对人数。
- 频道日志由独立频道客户端采集，不由监控客户端上传。
- 生产 OCR 使用 RapidOCR、ONNX Runtime DirectML 和 PP-OCRv6 medium 模型，无需打包 PaddleOCR、CUDA 或约 796 MB 的 `phi.dll`。

## 快速启动

创建 Python 环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动本地服务端：

```powershell
.\.venv\Scripts\python.exe -m app.server --host 127.0.0.1 --port 8765
```

使用 ONNX Runtime DirectML 启动监控客户端：

```powershell
$env:EVE_SENTRY_OCR_BACKEND = "onnx"
$env:EVE_SENTRY_ONNX_MODEL_DIR = "$PWD\.runtime\onnx-models"
.\scripts\start_monitor_client.ps1 `
  -Server http://127.0.0.1:8765 `
  -OcrDevice dml
```

也可以直接运行已经解压的发行包：

```powershell
.\EVE-Sentry-Monitor-ONNX\EVE-Sentry-Monitor.exe
```

发行包已经包含 Python 运行时、DirectML、ONNX Runtime 和 OCR 模型，目标电脑无需另外安装 Python 或模型。必须保留完整的 `_internal` 目录，不能只复制 EXE。

## 构建客户端

确认 `.runtime/onnx-models` 中存在检测与识别模型后执行：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  packaging\eve-sentry-monitor-onnx.spec
```

构建产物位于 `dist/EVE-Sentry-Monitor-ONNX/`。完整构建、压缩、模型校验和性能数据见 [ONNX OCR 验证与构建](onnx-ocr-validation.md)。旧 PaddleOCR GPU 兼容包仅用于回溯，说明见 [旧版客户端打包](monitor-client-packaging.md)。

## 测试

运行完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

OCR、红框定位、敌对人数、告警冷却、事件游标和文件状态相关改动应同时添加对应的回归测试。

## 文档索引

- [情报平台架构](intel-platform-architecture.md)：客户端、服务端、ESI、分类、告警、API 和存储设计。
- [情报工作流](intel-workflows.md)：OCR、频道情报、角色校验、敌我分类和告警流程。
- [实现路线图](intel-platform-roadmap.md)：当前完成度、边界和后续开发顺序。
- [配置 API](intel-config-api.md)：服务端分类、告警配置和运行时配置。
- [本地联调](local-integration.md)：启动顺序、健康检查、客户端状态和故障排查。
- [ONNX OCR 验证与构建](onnx-ocr-validation.md)：DirectML 性能、模型转换、客户端构建和验收。
- [旧版客户端打包](monitor-client-packaging.md)：PaddleOCR GPU 兼容包的构建与分发。
- [服务端部署](server-deployment.md)：Linux 部署、systemd、环境变量和客户端对接。
- [地图数据](map-data.md)：星图数据结构与生成方式。

## 历史文档

- `docs/superpowers/specs/2026-06-24-eve-sentry-design.md`
- `docs/superpowers/plans/2026-06-24-eve-sentry-plan.md`

以上两份文件记录早期单机 OCR 方案。当前行为以架构、工作流、联调和部署文档为准。
