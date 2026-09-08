# EVE Sentry 下载站

`download-site/` 是 EVE Sentry 单体仓库中的 Next.js 静态站点组件，提供客户端固定下载
入口和面向用户的客户端、服务端文档页面。下载 API、版本清单和 GitHub Release 附件代理
由同一仓库的 `deploy/cloudflare-download/` Worker 提供。

项目总览见[根 README](../README.md)，完整的发布拓扑、环境配置和上线验证见
[下载站开发与部署](../docs/download-site.md)。

## 本地开发

以下命令从仓库根目录执行：

```powershell
cd download-site
npm ci
npm run dev
```

开发服务器默认监听 `http://127.0.0.1:4174`。

## 构建与发布

```powershell
cd download-site
npm ci
npm run build
```

推送到 `main` 后，根目录 GitHub Actions 会按路径变更运行静态站构建、Worker 测试和
Wrangler 校验，并通过受保护的 `production` 环境发布。不要在组件目录创建独立发布流程。
