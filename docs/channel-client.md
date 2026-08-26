# 预警频道日志客户端

预警频道日志客户端是独立于 OCR 监控客户端的轻量 Windows 客户端。它只读取本机 EVE `Chatlogs` 中用户选定的频道日志，不读取游戏窗口、不截图、不控制游戏进程；每一行原始频道消息由服务端统一解析、去重和生成预警。

## 使用方式

开发环境可以在仓库根目录执行：

```powershell
.\scripts\start_channel_client.ps1
```

首次启动需要填写服务端地址。设备密钥可留空；填写时使用管理员创建的 `eve_...` 设备密钥。客户端会把密钥单独保存在 Windows 用户凭据保护文件中，不会写入普通配置 JSON。

1. 确认 EVE `Chatlogs` 目录。
2. 点击“刷新”，从发现的频道中多选预警频道。
3. 默认“首次启动忽略已有历史内容”，避免启动时把整份旧日志重新上报。
4. 点击“开始监听”。客户端会按文件字节偏移保存断点；重启后会继续读取尚未成功上报的内容。

## 配置和状态文件

默认位于 `%LOCALAPPDATA%\EVE Sentry\`：

- `channel_client_settings.json`：服务端地址、日志目录、频道选择和扫描间隔，不含密钥。
- `channel_client_auth.json`：受 Windows DPAPI 保护的设备密钥。
- `channel_client_offsets.json`：各频道日志文件的读取断点。

只有成功得到服务端响应后才提交断点。服务端不可用时会保留原行，恢复连接后自动重试。

## 打包

轻量客户端使用 `packaging/eve-sentry-channel.spec` 构建，不包含 OCR 模型：

```powershell
python -m PyInstaller --clean --noconfirm packaging\eve-sentry-channel.spec
```

输出目录为 `dist\EVE-Sentry-Channel`。它与 OCR 监控客户端是两个独立程序，可分别运行和更新。
