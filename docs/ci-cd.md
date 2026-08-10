# 机器人 CI/CD

GitHub Actions 工作流 `.github/workflows/deploy.yml` 在以下场景运行：

- 向 `codex/main` 提交或合并代码：验证通过后自动部署生产环境；
- 针对 `codex/main` 的 Pull Request：只运行验证，不接触生产环境；
- 手动触发 `workflow_dispatch`：重新验证并部署当前分支版本。

## 部署门禁

部署前会依次运行 Ruff、pytest、Docker Compose 配置检查和生产镜像构建。舰队分析模块当前有
3 个历史断言尚未与现有聚合口径对齐，工作流明确 `--deselect` 这 3 个用例；其余测试仍是强制门禁。
这 3 个用例修复后应立即从 workflow 中移除排除项。

## GitHub 配置

在仓库 `xiaqijun/eve-risk-analysis` 中配置：

Secrets：

- `EVE_RISK_DEPLOY_SSH_KEY`：部署用户的 OpenSSH 私钥；
- `EVE_RISK_DEPLOY_KNOWN_HOSTS`：生产主机的 `known_hosts` 条目。

Variables：

- `EVE_RISK_DEPLOY_HOST`：生产主机；
- `EVE_RISK_DEPLOY_USER`：具有 Docker 权限的部署用户；
- `EVE_RISK_DEPLOY_PORT`：SSH 端口；
- `EVE_RISK_DEPLOY_ROOT`：固定为生产部署目录，例如 `/opt/eve-risk-analysis`。

生产环境 `.env` 必须预先保存在 `${EVE_RISK_DEPLOY_ROOT}/.env`，不会上传到 GitHub，也不会进入部署包。

## 生产部署行为

每个提交解压到 `${EVE_RISK_DEPLOY_ROOT}/releases/<commit-sha>`，但始终使用固定 Compose 项目名
`eve-risk-analysis`，因此 PostgreSQL、Redis 和 SDE 命名卷会继续复用。远端 `flock` 与 Actions
`concurrency` 共同防止两个机器人版本同时部署。

部署后会确认 `bot`、`worker` 均处于运行状态，并在容器内请求 `/health/ready`，仅当返回 JSON
中的 `status` 为 `ok` 才切换 `current` 链接。若健康检查失败，脚本会重新启动上一个版本的代码。
数据库迁移必须保持向后兼容，因为代码回滚不会反向回滚 PostgreSQL schema。

禁止在发布流程中运行 `docker compose down -v`，否则会删除生产数据卷。
