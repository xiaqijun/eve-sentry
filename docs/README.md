# EVE Sentry 文档

这里保存 EVE Sentry 当前有效的开发、运行和部署文档。历史设计稿、阶段性路线图和已经
被当前实现替代的说明不再保留；功能行为以源码、测试和本目录文档为准。

## 阅读顺序

1. [系统架构](architecture.md)：了解客户端、服务端、Web、ESI、SDE 和事件流边界。
2. [监控客户端](client.md)：安装 ONNX/DirectML 环境、选择 EVE 窗口、配置密钥和打包。
3. [认证与 EVE 身份校验](authentication.md)：管理员、EVE SSO、设备密钥和 Listener 校验。
4. [Web 管理系统](web-console.md)：态势图、来袭报表、账号和管理员页面。
5. [API 参考](api-reference.md)：当前主要 HTTP 接口、SSE 和认证方式。
6. [服务端部署](server-deployment.md)：PostgreSQL、systemd、OpenResty/Nginx 和上线验证。

项目介绍和本地快速启动见仓库根目录 [README](../README.md)。

## 常用命令

监控客户端：

```powershell
$env:EVE_SENTRY_OCR_BACKEND = "onnx"
$env:EVE_SENTRY_OCR_DEVICE = "dml"
.\scripts\start_monitor_client.ps1 -Server http://127.0.0.1:8765
```

本地服务端：

```powershell
python -m app.server --host 127.0.0.1 --port 8765
```

前端：

```powershell
cd frontend
npm ci
npm run dev
```

测试：

```powershell
pytest
cd frontend
npm test
npm run build
```

## 当前约定

- 生产 OCR 使用 RapidOCR、ONNX Runtime DirectML 和 PP-OCRv6 medium 模型。
- 监控客户端只采集下拉框当前选中的 EVE 窗口。
- 监控和预警是独立开关，预警统一显示在客户端顶部浮窗。
- 普通用户使用 EVE SSO，管理员使用密码，桌面客户端使用设备密钥。
- 生产服务端使用 PostgreSQL；SQLite 仅用于本地开发和兼容。
- Web 由 React SPA 提供，Python 服务只提供 JSON API 和 SSE。
- zKillboard 不在当前生产链路，统计和界面不得构造不存在的数据。

## 文档维护

- 功能上线时直接更新对应主题文档，不新增同主题日期版计划文件。
- 接口变化同时更新 `api-reference.md` 和受影响的客户端或 Web 文档。
- 部署变量以 `deploy/linux/eve-sentry.env.example` 为准。
- 文档中的命令应能在当前仓库执行，过期兼容行为必须明确标注。
- 运行时数据库、token、设备密钥、日志、截图和模型缓存不写入文档目录。
