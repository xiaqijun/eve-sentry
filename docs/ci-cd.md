# CI/CD 与单体仓库边界

本仓库是服务端、客户端、QQ 机器人、ESI Gateway 和下载站的唯一开发源。`main` 是唯一
生产部署和客户端发布分支；push、Pull Request 和手动运行都可以执行验证，但 PR 与非
`main` 的手动运行不能进入部署或发布 job。有效工作流只位于根目录 `.github/workflows/`。

## 流水线

| 工作流 | 触发目录 | 作用 |
|---|---|---|
| `deploy-server.yml` | `app/`、`frontend/`、`scripts/`、`deploy/ci/`、`deploy/linux/`、服务端依赖 | 服务端测试、前端构建、PostgreSQL 集成、不可变打包和生产部署；下载 Worker 不进入服务端包 |
| `deploy-bot.yml` | `bot/` | 机器人 Ruff、单元测试和运行时配置校验；仅 `main` 非 PR 运行生产部署 |
| `ci-client.yml` | `client/` | Windows 客户端单元测试，以及独立进程中的真实 client-server 集成测试 |
| `ci-contracts.yml` | HTTP/SSE/事件相关服务端、客户端、机器人源码和协议文档 | 并行运行服务端协议、Windows client-server 与机器人契约测试；不部署 |
| `release-client.yml` | 成功的本仓库 `main` Client CI，或 `main` 手动运行 | 构建、签名并发布客户端；已取消任意 `v*` tag 触发 |
| `deploy-esi-gateway.yml` | `esi-gateway/` | Gateway 多 Python 版本验证、固定 Jita 上游 smoke、打包和生产部署 |
| `ci-download-site.yml` | `download-site/`、`deploy/cloudflare-download/` | 下载站构建、Worker 测试和 Wrangler dry-run |
| `deploy-download-site.yml` | 下载站、Worker 和公开文档 | 独立重复构建/验证静态站点，再部署 Cloudflare Worker |

`deploy-server.yml` 的 push 路径只包含服务端资产；目标为 `main` 的 PR 会运行完整服务端
验证。`deploy/cloudflare-download/` 既不触发服务端流程，也不打入服务端部署归档。
下载站 CI 与 Deploy 是两个由同一变更独立触发的工作流，Deploy 会自行重复 validate，
不会等待另一个 workflow 的结果。

## 触发条件与门禁

- 所有部署和客户端发布 job 都同时检查 `refs/heads/main`；PR 只验证，非 `main` 的
  `workflow_dispatch` 也不能绕过分支门禁。
- 路径过滤避免无关组件重复构建，但服务端 API、事件协议和根目录文档变化时，至少运行
  `Contract Compatibility` 的服务端、客户端和机器人兼容性测试。
- 服务端、机器人、Gateway 和下载站 deploy job 使用 `production` Environment；客户端
  release job 使用 `client-release`。两个 Environment 的自定义分支策略均只允许 `main`，
  当前未配置 required reviewers。
- 服务端和 Gateway 归档记录提交 SHA 与 SHA-256；机器人使用 `releases/<commit-sha>`；
  客户端清单带 Ed25519 签名、资产 SHA-256 和源码提交元数据。

生产发布保持并发锁，避免同一环境同时发布两个版本。服务端、客户端、契约和下载站 CI 会
取消同分支或同 PR 的旧运行；机器人、Gateway 和各生产部署按自身并发组串行或保留运行。
跨组件接口发生变化时，应在同一变更中更新 `docs/monorepo-development.md` 和 API 文档，
并完成对应契约测试。

## GitHub Actions Secrets / Variables

服务端：

- Secrets：`EVE_SENTRY_DEPLOY_SSH_KEY`、`EVE_SENTRY_DEPLOY_KNOWN_HOSTS`
- Variables：`EVE_SENTRY_DEPLOY_HOST`、`EVE_SENTRY_DEPLOY_USER`、`EVE_SENTRY_DEPLOY_PORT`、
  `EVE_SENTRY_PUBLIC_URL`

ESI Gateway：

- Secrets：`EVE_SENTRY_ESI_GATEWAY_SSH_KEY`、`EVE_SENTRY_ESI_GATEWAY_KNOWN_HOSTS`
- Variables：`EVE_SENTRY_ESI_GATEWAY_DEPLOY_HOST`、`EVE_SENTRY_ESI_GATEWAY_DEPLOY_USER`、
  `EVE_SENTRY_ESI_GATEWAY_DEPLOY_PORT`

客户端：

- Secret：`EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`
- GitHub 内置 `github.token` 用于 Release 和模型恢复。

机器人：

- Secrets：`EVE_RISK_DEPLOY_SSH_KEY`、`EVE_RISK_DEPLOY_KNOWN_HOSTS`、
  `EVE_RISK_POSTGRES_PASSWORD`、`EVE_RISK_REDIS_PASSWORD`
- Variables：`EVE_RISK_DEPLOY_HOST`、`EVE_RISK_DEPLOY_USER`、`EVE_RISK_DEPLOY_PORT`、
  `EVE_RISK_DEPLOY_ROOT`

生产 job 在建立 SSH 连接前会检查必需的 Secret/Variable、端口范围和公开 URL 格式；值本身
不会写入日志。下载站源码已纳入本仓库，客户端 Release、清单和 Worker 统一使用
`xiaqijun/eve-sentry` 作为发布源；下载域名保持 `evesentrydownload.kisectool.com` 不变。

下载站 workflow 引用 `CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_API_TOKEN` Actions
Secrets。推送 `main` 且命中下载站路径后会自动验证并部署；手动执行
`Download Site Deploy` 时必须选择 `main` 才会进入 deploy job。

## 验证与回滚

- 服务端远端部署脚本先检查本机 `/api/readyz`，失败时恢复最近备份；工作流再检查公开
  `${EVE_SENTRY_PUBLIC_URL}/api/readyz`。远端备份默认保留最近 5 次。
- Gateway 部署脚本要求 `/health` 返回 `ok=true`，且 `cache_entries` 是非负整数；主线
  发布前的 Python 3.13 job 还会对固定 Jita 星系最多重试三次真实上游 ESI smoke。部署
  健康检查失败时恢复 Gateway 备份并重启 systemd。
- 下载站验证首页、文档、`/health`、`latest.json`、302 跳转和 Range 206。Cloudflare
  部署完成后的公开验证失败不会自动恢复旧版本，需人工重新发布上一版本。
- 客户端发布清单必须签名并验证 SHA-256/Range；客户端更新器在启动健康检查失败时恢复
  旧安装目录。
- 机器人组件使用 immutable `releases/<commit-sha>` 和 `current` 链接回滚；数据库迁移
  不会反向回滚，schema 必须保持向后兼容。

生产验证失败时应保留工作流日志、发布 SHA、API 文档版本和上一版本标识，便于人工回滚和审计。
