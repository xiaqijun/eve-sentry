# CI/CD 与单体仓库边界

本仓库是服务端、客户端、QQ 机器人和 ESI Gateway 的唯一开发源。所有改动直接提交
到 `main`；推送后由根目录 `.github/workflows/` 按目录触发验证和部署，不创建功能分支
或 Pull Request。

## 流水线

| 工作流 | 触发目录 | 作用 |
|---|---|---|
| `deploy-server.yml` | `app/`、`frontend/`、`deploy/`、根目录依赖 | 服务端测试、前端构建、PostgreSQL 集成、打包和生产部署 |
| `ci-bot.yml` | `bot/` | 机器人 Ruff、单元测试和运行时配置校验；`main` 推送后进入生产部署 |
| `ci-client.yml` | `client/` | Windows 客户端测试、OCR 依赖和打包校验 |
| `release-client.yml` | `client/`、客户端版本或标签 | 构建、签名并发布客户端安装包 |
| `deploy-esi-gateway.yml` | `esi-gateway/` | Gateway 多 Python 版本验证、打包和生产部署 |

子目录中随导入保留的 `.github/workflows/` 不会被 GitHub 识别为 workflow，仅用于追溯
原仓库历史；修改 CI 时只编辑根目录 workflow。

## 触发条件与门禁

- 所有 workflow 必须只对 `main` 的 push 自动部署；`workflow_dispatch` 用于人工重跑。
- 路径过滤避免无关组件重复构建，但服务端 API、事件协议和根目录文档变化时，至少运行
  服务端、客户端和机器人兼容性测试。
- 所有部署先运行组件测试、静态检查和打包校验，再进入 `production` Environment。
- 发布包必须带提交 SHA 和 SHA-256 校验；部署失败由 role `90` 按服务端、机器人或 Gateway
  的回滚脚本恢复上一版本。

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

客户端：

- Secret：`EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`
- GitHub 内置 `github.token` 用于 Release 和模型恢复。

机器人：

- Secrets：`EVE_RISK_DEPLOY_SSH_KEY`、`EVE_RISK_DEPLOY_KNOWN_HOSTS`、
  `EVE_RISK_POSTGRES_PASSWORD`、`EVE_RISK_REDIS_PASSWORD`
- Variables：`EVE_RISK_DEPLOY_HOST`、`EVE_RISK_DEPLOY_USER`、`EVE_RISK_DEPLOY_PORT`、
  `EVE_RISK_DEPLOY_ROOT`

生产 job 在建立 SSH 连接前会检查必需的 Secret/Variable、端口范围和公开 URL 格式；值本身
不会写入日志。下载站仍是独立仓库和独立发布目标，本次单体化不把其代码导入本仓库。

## 验证与回滚

- 服务端远端部署脚本先检查本机 `/api/readyz`，失败时恢复最近备份；工作流再检查公开
  `${EVE_SENTRY_PUBLIC_URL}/api/readyz`。远端备份默认保留最近 5 次。
- Gateway `/health` 必须返回 `ok=true` 和 `cache_entries`；单体仓库 Gateway 流水线还应
  增加一次受控的上游 ESI smoke。部署失败时恢复 Gateway 备份并重启 systemd。
- 下载站验证首页、文档、`/health`、`latest.json`、302 跳转和 Range 206。Cloudflare
  部署完成后的公开验证失败不会自动恢复旧版本，需人工重新发布上一版本。
- 客户端发布清单必须签名并验证 SHA-256/Range；客户端更新器在启动健康检查失败时恢复
  旧安装目录。
- 机器人仓库使用 immutable `releases/<commit-sha>` 和 `current` 链接回滚；数据库迁移
  不会反向回滚，schema 必须保持向后兼容。

生产验证失败时应保留工作流日志、发布 SHA、API 文档版本和上一版本标识，便于人工回滚和审计。
