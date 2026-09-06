# 下载站开发与部署

下载站已经纳入 `eve-sentry` 单体仓库，包含两部分：

- `download-site/`：Next.js 静态页面和客户端/服务端文档页面；
- `deploy/cloudflare-download/`：Cloudflare Worker，负责 `/health`、`/latest.json`、
  `/download/latest` 和版本化下载，并代理 GitHub Release 附件。

客户端 Release 的唯一来源是 `xiaqijun/eve-sentry`。客户端发布工作流生成并签名
`latest.json`、程序包和 OCR 模型包，Worker 通过固定域名
`https://evesentrydownload.kisectool.com` 提供下载。这样代码、Release 和下载入口
不会再分散在多个仓库。首次迁移发布若尚无模型附件，流水线仅从旧客户端 Release
读取一次 OCR 模型作为引导，后续模型均从本仓库 Release 读取。

## 本地验证

```powershell
cd download-site
npm ci
npm run build
cd ..
node --test deploy/cloudflare-download/src/index.test.js
npx --yes wrangler@4.116.0 deploy --dry-run --config deploy/cloudflare-download/wrangler.jsonc
```

## 自动发布

推送 `main` 且变更命中下载站路径时：

1. `Download Site CI` 安装依赖、构建静态站点、运行 Worker 测试和 Wrangler dry-run；
2. `Download Site Deploy` 使用 `production` Environment 的 Cloudflare 凭据部署 Worker；
3. 部署后验证首页、文档、`/health`、`/latest.json`、最新版本跳转和 Range 下载。

需要配置以下 Environment Secrets：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

若只需重跑部署，可在 GitHub Actions 手动执行 `Download Site Deploy`，无需创建分支或
Pull Request。
