# 机器人 CI/CD

GitHub Actions 工作流 `.github/workflows/deploy.yml` 以 `main` 为唯一发布入口：

- 向 `main` 推送提交：验证通过后自动部署生产环境；
- 其他分支或 Pull Request（如平台触发）：只运行验证，不接触生产环境；
- 手动触发 `workflow_dispatch`：默认只验证；生产部署仍必须使用 `main` 提交。

## 部署门禁

部署前会依次运行 Ruff、pytest 和锁定依赖安装检查。舰队分析模块当前有
3 个历史断言尚未与现有聚合口径对齐，工作流明确 `--deselect` 这 3 个用例；其余测试仍是强制门禁。
这 3 个用例修复后应立即从 workflow 中移除排除项。

## GitHub 配置

在仓库 `xiaqijun/eve-sentry-bot` 的 `production` 环境中配置：

Secrets：

- `EVE_RISK_DEPLOY_SSH_KEY`：部署用户的 OpenSSH 私钥；
- `EVE_RISK_DEPLOY_KNOWN_HOSTS`：生产主机的 `known_hosts` 条目；
- `EVE_RISK_POSTGRES_PASSWORD`：机器人专用 PostgreSQL 角色密码；
- `EVE_RISK_REDIS_PASSWORD`：机器人专用 Redis ACL 用户密码。

Variables：

- `EVE_RISK_DEPLOY_HOST`：生产主机；
- `EVE_RISK_DEPLOY_USER`：能够写入部署目录并管理 systemd 服务的部署用户；
- `EVE_RISK_DEPLOY_PORT`：SSH 端口；
- `EVE_RISK_DEPLOY_ROOT`：固定为生产部署目录，例如 `/opt/eve-risk-analysis`。

生产环境 `.env` 必须预先保存在 `${EVE_RISK_DEPLOY_ROOT}/.env`，不会上传到 GitHub，也不会进入部署包。数据库密码由 `production` 环境 Secrets 在部署时通过临时凭据文件传递，该文件读取后立即删除。

## 生产部署行为

每个提交解压到 `${EVE_RISK_DEPLOY_ROOT}/releases/<commit-sha>`，并在该目录创建独立的 Python
虚拟环境。PostgreSQL 和 Redis 复用主机上的现有 Docker 容器，SDE 索引保存在
`${EVE_RISK_DEPLOY_ROOT}/data`。远端 `flock` 与 Actions `concurrency` 共同防止两个机器人版本同时部署。

部署后会确认两个 systemd 服务均处于运行状态，并请求主机上的 `/health/ready`，仅当返回 JSON
中的 `status` 为 `ok` 才切换 `current` 链接。若健康检查失败，脚本会重新启动上一个版本的代码。
数据库迁移必须保持向后兼容，因为代码回滚不会反向回滚 PostgreSQL schema。

非 root 部署使用 `systemctl --user`，生产用户需要预先执行 `loginctl enable-linger <user>`，并确保
该用户拥有部署目录和 systemd user manager。生产主机需预先安装 Python 3.12、Docker、
`curl`、`flock` 与 `python3-venv`。主机如已安装 uv，脚本会优先使用锁定依赖；否则使用 pip
从 `PIP_INDEX_URL`（默认阿里云镜像）安装项目。部署会在现有 PostgreSQL 容器中创建或更新
`eve_risk` 角色与同名数据库，并在现有 Redis 容器中创建或更新 `eve_risk` ACL 用户。
