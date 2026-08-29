# CI/CD 与仓库边界

本文说明 `eve-sentry` 主服务仓库当前的流水线，以及拆分为五个公开仓库后的发布边界。
生产部署只允许来自默认分支的合并提交；Pull Request 只执行验证，不连接生产环境。

## 仓库归属

| 仓库 | 职责 | 生产入口 |
|---|---|---|
| `xiaqijun/eve-sentry` | 情报服务、Web Console、下载站编排 | `deploy-server.yml`、`deploy-download-site.yml` |
| `xiaqijun/eve-sentry-bot` | QQ 机器人、分析 Worker、Docker Compose | 机器人仓库自己的 `deploy.yml` |
| `xiaqijun/eve-sentry-client` | Windows OCR/监控客户端和更新器 | 客户端仓库自己的 `release-client.yml` |
| `xiaqijun/eve-sentry-esi-gateway` | 独立 ESI Gateway | Gateway 仓库自己的 `deploy-esi-gateway.yml` |
| `xiaqijun/eve-sentry-contracts` | HTTP、SSE、JSON Schema 和兼容性 fixture | 只发布契约包，不直接部署服务 |

拆分完成前，本仓库仍保留兼容性的 `deploy-esi-gateway.yml`、`release-client.yml` 和下载站
目录；这些路径只作为迁移期流水线，不代表新仓库的最终代码归属。

## 触发条件与门禁

- `deploy-server.yml`：`main` 的服务端运行时路径变化时触发 `push`；同一组路径的
  Pull Request 只运行 Python、前端和 PostgreSQL 验证。生产 job 仅接受 `main`，并使用
  `production` Environment。
- `deploy-esi-gateway.yml`：Gateway 代码、服务单元或部署脚本变化时触发；Pull Request
  运行 Gateway 测试，生产 job 仅接受 `main`。
- `ci-client.yml`：客户端代码、打包脚本、依赖、资源或测试变化时运行 Windows 测试，
  适用于 push、Pull Request 和手动验证。
- `release-client.yml`：当前仍由 `app/version.py` 版本变更或手动触发，负责签名和发布
  GitHub Release；客户端拆分后迁移到 `eve-sentry-client`。
- `deploy-download-site.yml`：下载站或 Worker 路径变化时，Pull Request 运行构建、Worker
  测试和 Wrangler dry-run；合并到 `main` 后才部署 Cloudflare 并执行公开 smoke。
- `workflow_dispatch` 可以用于重跑验证，但生产 job 会拒绝非默认分支。

所有生产 job 都应保持并发锁，避免同一环境同时发布两个版本。契约仓库发布新版本后，
消费者仓库必须先通过契约兼容性矩阵，再进入生产发布。

## GitHub Secrets / Variables

服务端：

- Secrets：`EVE_SENTRY_DEPLOY_SSH_KEY`、`EVE_SENTRY_DEPLOY_KNOWN_HOSTS`
- Variables：`EVE_SENTRY_DEPLOY_HOST`、`EVE_SENTRY_DEPLOY_USER`、`EVE_SENTRY_DEPLOY_PORT`、
  `EVE_SENTRY_PUBLIC_URL`

ESI Gateway：

- Secrets：`EVE_SENTRY_ESI_GATEWAY_SSH_KEY`、`EVE_SENTRY_ESI_GATEWAY_KNOWN_HOSTS`
- Variables：`EVE_SENTRY_ESI_GATEWAY_DEPLOY_HOST`、`EVE_SENTRY_ESI_GATEWAY_DEPLOY_USER`、
  `EVE_SENTRY_ESI_GATEWAY_DEPLOY_PORT`

下载站：

- Secrets：`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_API_TOKEN`
- Wrangler 配置中的 `GITHUB_OWNER` 和 `GITHUB_REPO` 必须指向客户端发布仓库。

客户端：

- Secret：`EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`
- GitHub 内置 `github.token` 用于 Release 和模型恢复。

生产 job 在建立 SSH 连接前会检查必需的 Secret/Variable、端口范围和公开 URL 格式；值本身
不会写入日志。

## 验证与回滚

- 服务端远端部署脚本先检查本机 `/api/readyz`，失败时恢复最近备份；工作流再检查公开
  `${EVE_SENTRY_PUBLIC_URL}/api/readyz`。远端备份默认保留最近 5 次。
- Gateway `/health` 必须返回 `ok=true` 和 `cache_entries`；拆分后的 Gateway 流水线还应
  增加一次受控的上游 ESI smoke。部署失败时恢复 Gateway 备份并重启 systemd。
- 下载站验证首页、文档、`/health`、`latest.json`、302 跳转和 Range 206。Cloudflare
  部署完成后的公开验证失败不会自动恢复旧版本，需人工重新发布上一版本。
- 客户端发布清单必须签名并验证 SHA-256/Range；客户端更新器在启动健康检查失败时恢复
  旧安装目录。
- 机器人仓库使用 immutable `releases/<commit-sha>` 和 `current` 链接回滚；数据库迁移
  不会反向回滚，schema 必须保持向后兼容。

生产验证失败时应保留工作流日志、发布 SHA、契约版本和上一版本标识，便于人工回滚和审计。
