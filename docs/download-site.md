# 下载站开发与部署

下载站已经纳入 `eve-sentry` 单体仓库，包含两部分：

- `download-site/`：Next.js 静态页面和客户端/服务端文档页面；
- `deploy/cloudflare-download/`：Cloudflare Worker，负责 `/health`、`/latest.json`、
  `/download/latest` 和版本化下载，并代理 GitHub Release 附件。

客户端 Release 的唯一来源是 `xiaqijun/eve-sentry`。客户端发布工作流生成程序包和 OCR
模型包，在 `latest.json` 中记录各包 SHA-256，并对清单做 Ed25519 签名。Worker 通过固定域名
`https://evesentrydownload.kisectool.com` 提供下载。这样代码、Release 和下载入口
不会再分散在多个仓库。OCR 模型恢复也只允许读取本仓库已有的客户端 Release；若找不到
可用的模型附件，发布会直接失败，不再回退到已经废弃的独立客户端仓库。

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

推送 `main` 且变更命中 `download-site/**`、`deploy/cloudflare-download/**`、公开客户端或
服务端文档，以及两份下载站 workflow 时：

1. `Download Site CI` 安装依赖、构建静态站点、运行 Worker 测试和 Wrangler dry-run；
2. `Download Site Deploy` 独立重复同一套验证，并通过构建产物在 `validate` 与 `deploy`
   job 之间传递静态站点；
3. `deploy` job 只在 `main` 上运行，使用 `production` Environment 和仓库 Actions Secrets
   中的 Cloudflare 凭据部署 Worker；
4. 部署后验证首页、文档、`/health`、`/latest.json`、最新版本跳转和 Range 下载。

需要配置以下仓库 Actions Secrets：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

`production` Environment 只允许 `main` 分支，没有人工审批。若只需重跑部署，可在
`main` 上手动执行 `Download Site Deploy`；非 `main` 分支只能运行验证，不会进入部署 job。

下载站与服务端使用不同的发布边界。服务端制品只打包 `deploy/ci` 与 `deploy/linux`；
`deploy/cloudflare-download/` 只由下载站工作流部署，不会随服务端制品上传。
