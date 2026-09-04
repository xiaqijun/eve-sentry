# CI/CD 与仓库边界

本文说明各仓库当前的流水线和发布边界。项目约定后续直接在 `main` 开发并推送，
由对应仓库的 CI/CD 自动验证和部署；不创建额外开发分支或 Pull Request。若平台保留
Pull Request 触发器，它只作为兼容性的只读验证入口，不改变 `main` 的发布路径。

## 仓库归属

| 仓库 | 职责 | 生产入口 |
|---|---|---|
| `xiaqijun/eve-sentry` | 情报服务、Web Console | `deploy-server.yml` |
| `xiaqijun/eve-sentry-bot` | QQ 机器人、分析 Worker、Docker Compose | 机器人仓库自己的 `deploy.yml` |
| `xiaqijun/eve-sentry-client` | Windows OCR/监控客户端和更新器 | 客户端仓库自己的 `release-client.yml` |
| `xiaqijun/eve-sentry-esi-gateway` | 独立 ESI Gateway | Gateway 仓库自己的 `deploy-esi-gateway.yml` |
| `xiaqijun/eve-sentry-download-site` | 静态下载站和 Cloudflare 下载 Worker | 下载站仓库自己的 `deploy.yml` |

本仓库已退役客户端 CI、客户端 Release、下载站部署和独立 Gateway 部署入口，避免
拆仓后同一组件被两个仓库重复发布。服务端只保留 `deploy-server.yml`。

## 触发条件与门禁

- `deploy-server.yml`：`main` 推送时运行 Python、前端、PostgreSQL、源码编译和部署脚本
  语法验证；验证通过后自动部署。保留的 Pull Request 触发器只运行同一套门禁，不部署生产。
  验证通过后先生成带 SHA-256 校验的不可变部署包，再由生产 job 复用该包发布。生产 job
  仅接受 `main`，并使用 `production` Environment。
- `eve-sentry-esi-gateway/.github/workflows/deploy-esi-gateway.yml`：独立 Gateway 的
  Pull Request 和 `main` push 自动测试；`main` push 通过验证后自动进入 production Environment。
- `eve-sentry-client/.github/workflows/ci-client.yml`：客户端 Pull Request 和 `main` push 自动测试。
- 客户端 Release 由 `eve-sentry-client/.github/workflows/release-client.yml` 负责；本仓库不再
  构建或发布客户端。
- 下载站由 `eve-sentry-download-site` 的 `CI` 和 `Deploy` workflow 负责。生产部署验证首页、
  Worker、客户端仓库 `latest.json`、302 跳转和 Range 206。
- `eve-sentry/.github/workflows/deploy-server.yml`：服务端实现与跨仓库联动文档一起验证服务端测试、
  构建和部署脚本。
- `eve-sentry-bot/.github/workflows/deploy.yml`：`main` 推送自动验证并进入 production
  Environment；其他分支或 Pull Request（如触发）仅验证。
- `workflow_dispatch` 可以用于重跑验证；生产 job 只接受默认分支。下载站按仓库规则保持
  production 部署手动触发，避免 Cloudflare Worker 被无审批覆盖。

生产发布保持并发锁，避免同一环境同时发布两个版本；同一 Pull Request 的旧验证会自动取消，
不同 PR 之间互不阻塞。跨仓库接口发生变化时，先更新服务端
`docs/multi-repository-development.md` 和 API 文档，再分别在消费者仓库完成联调。

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
- Secrets 应配置到 `eve-sentry-download-site`；首次拆仓部署由本仓库一次性 bridge 使用旧
  Secret 完成，独立仓库 Secret 配置前不得手动触发后续生产部署。
- Wrangler 的 `GITHUB_OWNER=xiaqijun`、`GITHUB_REPO=eve-sentry-client` 指向客户端
  Release 仓库。

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

生产验证失败时应保留工作流日志、发布 SHA、API 文档版本和上一版本标识，便于人工回滚和审计。
