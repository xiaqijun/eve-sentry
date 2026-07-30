# 服务端部署

当前生产结构：

```text
OpenResty/Nginx :80/:443
  -> frontend/dist
  -> /api/ -> Python 127.0.0.1:8765
Python app.server
  -> PostgreSQL
  -> ESI / SDE
```

## 目录

```text
/opt/eve-sentry/                       仓库和服务端虚拟环境
/etc/eve-sentry/eve-sentry.env         环境配置
/var/lib/eve-sentry/                   运行数据、SDE、ESI token/cache
/opt/1panel/www/eve-sentry/            当前 1Panel/OpenResty 静态目录
```

## 安装

```bash
sudo useradd --system --home /opt/eve-sentry --shell /usr/sbin/nologin eve-sentry
sudo mkdir -p /opt/eve-sentry /etc/eve-sentry /var/lib/eve-sentry
sudo chown -R eve-sentry:eve-sentry /opt/eve-sentry /var/lib/eve-sentry

sudo -u eve-sentry git clone YOUR_REPOSITORY /opt/eve-sentry
cd /opt/eve-sentry
sudo -u eve-sentry python3 -m venv .venv-server
sudo -u eve-sentry .venv-server/bin/python -m pip install --upgrade pip
sudo -u eve-sentry .venv-server/bin/python -m pip install -r requirements-server.txt
```

服务端依赖不包含 PyQt、截图和 OCR。PostgreSQL 驱动使用
`psycopg[binary,pool]>=3.2.0`。

## PostgreSQL

```bash
sudo -u postgres createuser --pwprompt eve_sentry
sudo -u postgres createdb --owner eve_sentry eve_sentry
```

生产配置：

```dotenv
EVE_SENTRY_SERVER_STORAGE=postgres
EVE_SENTRY_SERVER_POSTGRES_DSN=postgresql://eve_sentry:CHANGE_ME@127.0.0.1:5432/eve_sentry
```

服务端启动时自动创建和迁移表。升级前仍应备份数据库：

```bash
sudo -u postgres pg_dump -Fc eve_sentry > /var/lib/eve-sentry/eve_sentry.dump
```

连接池默认最小 2、最大 8 个连接。不要为每个 SSE 心跳重新创建 PostgreSQL 连接。

## 环境配置

```bash
sudo cp deploy/linux/eve-sentry.env.example /etc/eve-sentry/eve-sentry.env
sudo chown root:eve-sentry /etc/eve-sentry/eve-sentry.env
sudo chmod 640 /etc/eve-sentry/eve-sentry.env
```

重点字段：

```dotenv
EVE_SENTRY_SERVER_HOST=127.0.0.1
EVE_SENTRY_SERVER_PORT=8765
EVE_SENTRY_SERVER_STORAGE=postgres
EVE_SENTRY_SERVER_POSTGRES_DSN=postgresql://eve_sentry:CHANGE_ME@127.0.0.1:5432/eve_sentry
EVE_SENTRY_SERVER_HOT_REPORT_LIMIT=5000
EVE_SENTRY_SERVER_REPORT_RETENTION_DAYS=0
EVE_SENTRY_SERVER_INACTIVE_INTEL_RETENTION_DAYS=30
EVE_SENTRY_SERVER_CONFIG=/var/lib/eve-sentry/intel_config.json
EVE_SENTRY_SERVER_AUTH_MODE=setup
EVE_SENTRY_SERVER_MAP_SOURCE=sde
EVE_SENTRY_SERVER_MAP_SDE_PATH=/var/lib/eve-sentry/sde/BUILD_NUMBER
EVE_SENTRY_SERVER_MAP_REGION_IDS=10000045
EVE_SENTRY_SERVER_ESI_CLIENT_ID=YOUR_EVE_APP_CLIENT_ID
EVE_SENTRY_SERVER_ESI_REDIRECT_URI=http://YOUR_SERVER/api/v1/auth/esi/callback
EVE_SENTRY_SERVER_ESI_TOKEN_FILE=/var/lib/eve-sentry/esi_tokens.json
EVE_SENTRY_SERVER_ESI_TOKEN_STORAGE=plain
```

`EVE_SENTRY_SERVER_REPORT_RETENTION_DAYS` 默认为 `0`，不会自动删除历史。设为正整数后，
服务端每次启动会按 `received_at` 删除超出窗口的报告；仍被活跃情报引用的报告会保留。
启用前先备份数据库。PostgreSQL 使用批量删除，JSON 存储会重写保留后的文件。

`EVE_SENTRY_SERVER_INACTIVE_INTEL_RETENTION_DAYS` 默认为 `30`。PostgreSQL 启动时只会
删除超过窗口的 inactive 情报行；活跃情报及其引用的历史报告不会被删除。设为 `0` 可关闭。

认证不依赖 HTTPS 才能启用。HTTP 仅适合可信网络；公网建议配置 TLS，并把回调地址、
客户端地址和机器人地址统一切换为 HTTPS。

## SDE 地图

```bash
cd /opt/eve-sentry
sudo -u eve-sentry .venv-server/bin/python scripts/sync_sde.py \
  --target /var/lib/eve-sentry/sde
```

`EVE_SENTRY_SERVER_MAP_SDE_PATH` 指向解压后的 SDE 根目录。Tenal 的 region ID 为
`10000045`。配置区域外的情报仍会存储，但不会自动扩展当前星图拓扑。

## EVE SSO

普通用户登录和态势页 ESI 授权共用一个 EVE 应用及回调：

```dotenv
EVE_SENTRY_SERVER_ESI_REDIRECT_URI=http://YOUR_SERVER/api/v1/auth/esi/callback
EVE_SENTRY_SERVER_ESI_SCOPES=esi-location.read_location.v1,esi-characters.read_contacts.v1,esi-corporations.read_contacts.v1,esi-alliances.read_contacts.v1,esi-search.search_structures.v1
```

普通用户登录只接受允许军团中的角色。态势页授权需要的 scopes 由同一回调根据 OAuth
state 区分。不要提交 `esi_tokens.json`。

## systemd

```bash
sudo cp deploy/linux/eve-sentry.service /etc/systemd/system/eve-sentry.service
sudo systemctl daemon-reload
sudo systemctl enable --now eve-sentry
sudo systemctl status eve-sentry
```

服务实际入口：

```text
/opt/eve-sentry/.venv-server/bin/python /opt/eve-sentry/scripts/run_server.py
```

日志和重启：

```bash
sudo journalctl -u eve-sentry -f
sudo systemctl restart eve-sentry
```

服务端为每个请求输出一条 INFO 级 JSON 访问记录，字段包括 `request_id`、方法、无查询
参数的路径、状态码和耗时。所有响应同时返回 `X-Request-ID`；仓库提供的 Nginx 模板会
用 `$request_id` 串联代理与应用日志。访问记录不会写入查询参数、认证头、Cookie 或请求体。

## 前端与反向代理

```bash
cd /opt/eve-sentry/frontend
npm ci
npm test
npm run build
```

将 `frontend/dist/` 同步到站点静态目录。Nginx/OpenResty 需要：

- `/assets/` 使用带 immutable 的长期缓存。
- `/api/` 反向代理到 `http://127.0.0.1:8765`。
- `/api/v1/events` 和 `/api/events` 关闭 buffering 和 cache。
- 其他路径使用 `try_files ... /index.html` 支持 React Router。
- HTTPS 代理传递 `X-Forwarded-Proto`，使会话 Cookie 获得 `Secure` 属性。

仓库提供 `deploy/linux/eve-sentry.nginx.conf`。Windows 开发机可执行完整部署脚本：

```powershell
.\scripts\deploy_frontend.ps1 `
  -Target root@YOUR_SERVER `
  -IdentityFile "$HOME\.ssh\eve_server_key" `
  -PublicUrl http://YOUR_SERVER
```

脚本会安装依赖、测试、构建、上传、备份、同步并执行健康检查；失败时恢复本轮备份。

## 上线顺序

1. 备份 PostgreSQL 和运行配置。
2. 更新代码并安装 `requirements-server.txt`。
3. 使用 `setup` 模式创建初始管理员。
4. 配置允许军团和必要的用户角色白名单。
5. 配置 EVE SSO 和 QQ 机器人只读服务密钥。
6. 升级桌面客户端并完成身份校验。
7. 切换到 `enforce`，重启服务并验证健康、登录、OCR、心跳和 SSE。

## 自动部署

`.github/workflows/deploy-server.yml` 在每次 `main` 推送后执行完整 Python 测试、前端
测试和生产构建。验证通过后，工作流上传单一部署归档并调用
`deploy/ci/deploy_release.sh`。远端流程使用部署锁，备份受管理的后端文件、前端静态
目录和 systemd 单元，安装服务端依赖并重启；就绪检查失败时自动恢复备份。默认保留
最近 5 次部署备份。

GitHub Actions 配置：

| 类型 | 名称 | 用途 |
|---|---|---|
| Secret | `EVE_SENTRY_DEPLOY_SSH_KEY` | 服务端 SSH 私钥 |
| Secret | `EVE_SENTRY_DEPLOY_KNOWN_HOSTS` | 固定 SSH 主机指纹 |
| Variable | `EVE_SENTRY_DEPLOY_HOST` | 部署主机 |
| Variable | `EVE_SENTRY_DEPLOY_USER` | SSH 用户 |
| Variable | `EVE_SENTRY_DEPLOY_PORT` | SSH 端口 |
| Variable | `EVE_SENTRY_PUBLIC_URL` | 部署后的公开站点根地址 |

日常部署不需要人工操作。只有工作流失败时，才使用 `workflow_dispatch` 重跑或查看远端
备份和服务日志。

## 验证

```bash
curl http://127.0.0.1:8765/api/livez
curl http://127.0.0.1:8765/api/readyz
curl http://127.0.0.1:8765/api/health
curl -H "Authorization: Bearer $EVE_SENTRY_API_KEY" \
  http://127.0.0.1:8765/api/v1/clients
curl -N -H "Authorization: Bearer $EVE_SENTRY_API_KEY" \
  'http://127.0.0.1:8765/api/v1/events?timeout=10&heartbeat=5'
```

`scripts/integration_status_check.py` 可用于认证关闭或 `setup` 迁移期的只读现场检查；
当前脚本不接收 API 密钥，`enforce` 模式应以上述带 Bearer 的 `curl` 检查为准。
