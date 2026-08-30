# GitCode 镜像状态

GitCode 客户端发布镜像目前已暂停。Windows 客户端版本由
`xiaqijun/eve-sentry-client` 仓库的 Release workflow 构建并发布到 GitHub Release，
更新清单由 `xiaqijun/eve-sentry-download-site` 的 Cloudflare Worker 托管。

客户端使用 `latest.json` 中的主下载地址下载程序和 OCR 模型，并在安装前校验 Ed25519
签名、文件大小和 SHA-256。当前发布清单中的 `mirrors` 数组为空，不会请求 GitCode。

暂停原因是 GitCode Release 附件上传在 GitHub Actions 环境中可能长时间无响应，影响正常
客户端版本发布。相关发布脚本和可选镜像支持暂时保留，但发布工作流不再传入 GitCode
仓库或令牌，也不再等待 GitCode 上传与下载校验。

恢复镜像前，应先验证附件上传稳定性、匿名范围下载（`206 Partial Content`）以及完整发布
流程，再重新启用工作流中的镜像步骤。
