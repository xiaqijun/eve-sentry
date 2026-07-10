# EVE Sentry 服务端部署指南

> Current status (2026-07-10): Python intel server is API/SSE only. The old
> embedded HTML page has been removed; production UI is the React SPA under
> `frontend/dist`, served by OpenResty/Nginx with `/api/` proxied to Python.

> Current status (2026-07-09): zKillboard/killboard enrichment is disabled and
> should not be configured on the server. Remove old zKill cache files from the
> runtime directory during deployment to avoid loading stale large JSON caches.

> 日期: 2026-07-01

这份文档只覆盖情报服务端部署。桌面 OCR 检测端、频道采集器和预警客户端仍然运行在本地机器上，通过 HTTP 连接远端服务端。

## 部署内容

- `app.server`: HTTP JSON API、SSE 事件流
- `frontend/dist`: React 情报工作台静态资源
- OpenResty/Nginx 统一入口: `/` 托管 React，`/api/` 反向代理 Python
- PostgreSQL 存储、SQLite 兼容存储和运行期数据文件
- 可选 ESI 补全
- 可选 zKillboard 补全

当前服务端路径不依赖 `requirements.txt` 里的 GUI、OCR 和 Windows 抓图依赖，服务器上使用 `requirements-server.txt` 即可。

## 推荐目录布局

```text
/opt/eve-sentry/                  仓库目录
/opt/eve-sentry/.venv-server/     服务端虚拟环境
/etc/eve-sentry/eve-sentry.env    服务端环境变量
/var/lib/eve-sentry/              runtime data、ESI token/cache、SDE 数据
```

## 1. 准备主机

安装 Python 3.11+，并准备运行目录:

```bash
sudo useradd --system --home /opt/eve-sentry --shell /usr/sbin/nologin eve-sentry
sudo mkdir -p /opt/eve-sentry /etc/eve-sentry /var/lib/eve-sentry
sudo chown -R eve-sentry:eve-sentry /opt/eve-sentry /var/lib/eve-sentry
```

拉代码并创建服务端虚拟环境:

```bash
sudo -u eve-sentry git clone <your-repo-url> /opt/eve-sentry
cd /opt/eve-sentry
sudo -u eve-sentry python3 -m venv .venv-server
sudo -u eve-sentry .venv-server/bin/python -m pip install --upgrade pip
sudo -u eve-sentry .venv-server/bin/python -m pip install -r requirements-server.txt
```

## 2. 配置环境变量

复制模板并按实际路径修改:

```bash
sudo cp deploy/linux/eve-sentry.env.example /etc/eve-sentry/eve-sentry.env
sudo chown root:root /etc/eve-sentry/eve-sentry.env
sudo chmod 640 /etc/eve-sentry/eve-sentry.env
```

至少检查这些字段:

- `EVE_SENTRY_SERVER_HOST`
- `EVE_SENTRY_SERVER_PORT`
- `EVE_SENTRY_SERVER_STORAGE`
- `EVE_SENTRY_SERVER_POSTGRES_DSN`
- `EVE_SENTRY_SERVER_DB`
- `EVE_SENTRY_SERVER_CONFIG`
- `EVE_SENTRY_SERVER_MAP_SOURCE`
- `EVE_SENTRY_SERVER_MAP_SDE_PATH`
- `EVE_SENTRY_SERVER_MAP_REGION_IDS`
- `EVE_SENTRY_SERVER_ENABLE_ESI`

生产环境推荐:

```dotenv
EVE_SENTRY_SERVER_STORAGE=postgres
EVE_SENTRY_SERVER_POSTGRES_DSN=postgresql://eve_sentry:<password>@127.0.0.1:5432/eve_sentry
```

`EVE_SENTRY_SERVER_DB` 仍保留给 SQLite 本地联调和回退使用。
健康检查会返回脱敏后的 PostgreSQL DSN，不会暴露密码。

如果地图使用官方 SDE，先在服务器上同步一次 YAML 地图表，再启动服务:

```bash
cd /opt/eve-sentry
sudo -u eve-sentry .venv-server/bin/python scripts/sync_sde.py \
  --build 3417089 \
  --target /var/lib/eve-sentry/sde
```

`Tenal` 的官方 region id 是 `10000045`，可直接写到
`EVE_SENTRY_SERVER_MAP_REGION_IDS=10000045`。
服务端按这个配置启动后，星图拓扑会固定在配置区域内；其他星系的
预警/OCR 情报会继续入库和出现在情报列表里，但不会自动新增成星图节点。

如果启用 authenticated ESI，Linux 上建议:

- `EVE_SENTRY_SERVER_ESI_TOKEN_STORAGE=plain`
- `EVE_SENTRY_SERVER_ESI_TOKEN_FILE=/var/lib/eve-sentry/esi_tokens.json`

然后用文件权限保护 token 文件，而不是依赖 Windows DPAPI。

## 3. 先前台启动一次

在启用 systemd 之前，先用同一个入口手工启动:

```bash
cd /opt/eve-sentry
sudo -u eve-sentry env $(grep -v '^#' /etc/eve-sentry/eve-sentry.env | xargs) \
  .venv-server/bin/python scripts/run_server.py
```

健康检查:

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/v1/clients
```

`http://127.0.0.1:8765/` 不再返回页面；根路径由 OpenResty/Nginx 托管
React SPA，Python 根路径返回 API 404。

## 4. 启用 systemd

安装服务文件:

```bash
sudo cp deploy/linux/eve-sentry.service /etc/systemd/system/eve-sentry.service
sudo systemctl daemon-reload
sudo systemctl enable --now eve-sentry
```

常用命令:

```bash
sudo systemctl status eve-sentry
sudo journalctl -u eve-sentry -f
sudo systemctl restart eve-sentry
```

## 5. 配置 OpenResty/Nginx 统一入口

生产环境推荐由 OpenResty 或 Nginx 统一对外暴露 React 工作台和 Python API：

- `/` -> `frontend/dist`
- `/api/` -> `http://127.0.0.1:8765`

先在本地构建前端：

```bash
cd frontend
npm install
npm run build
```

然后把构建产物同步到服务器，例如：

```bash
sudo mkdir -p /var/www/eve-sentry/frontend
sudo rsync -av --delete frontend/dist/ /var/www/eve-sentry/frontend/
```

如果使用系统 Nginx，安装并启用：

```bash
sudo apt-get update
sudo apt-get install -y nginx
sudo cp deploy/linux/eve-sentry.nginx.conf /etc/nginx/sites-available/eve-sentry
sudo ln -sf /etc/nginx/sites-available/eve-sentry /etc/nginx/sites-enabled/eve-sentry
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

如果服务器使用 1Panel/OpenResty，可把同等 `server` 配置放到站点
`conf.d` 中，并把静态目录挂载到 OpenResty 容器内。当前生产约定为：

- React 静态目录: `/opt/1panel/www/eve-sentry`
- 站点配置: `/opt/1panel/www/conf.d/eve-sentry.conf`
- 后端 API: `http://127.0.0.1:8765`

`deploy/linux/eve-sentry.nginx.conf` 默认会把：

- `root` 指向 `/var/www/eve-sentry/frontend`
- `/api/v1/events` 关闭 buffering，保证 SSE 实时推送；旧 `/api/events` 仅用于兼容旧客户端
- 其他 `/api/` 请求反代到本地 intel server
- 其余路径回退到 `index.html`，支持 React Router SPA 路由

如果服务已经直接对外暴露 `8765`，切换完成后建议在安全组或防火墙层收口，只保留 OpenResty/Nginx 对外入口。

## 6. authenticated ESI 登录

先确认环境文件里已经配置 authenticated ESI 的运行路径和 scopes:

```bash
EVE_SENTRY_SERVER_ESI_CLIENT_ID=YOUR_EVE_APP_CLIENT_ID
EVE_SENTRY_SERVER_ESI_REDIRECT_URI=http://127.0.0.1:8766/callback
EVE_SENTRY_SERVER_ESI_TOKEN_FILE=/var/lib/eve-sentry/esi_tokens.json
EVE_SENTRY_SERVER_ESI_TOKEN_STORAGE=plain
EVE_SENTRY_SERVER_ESI_SCOPES=esi-location.read_location.v1,esi-characters.read_contacts.v1
```

远端无浏览器环境推荐用 SSH 隧道完成 localhost 回调。先在本地终端打开隧道:

```bash
ssh -L 8766:127.0.0.1:8766 eve-sentry@YOUR_SERVER
```

再在服务器 shell 上做一次登录并保存 token:

```bash
cd /opt/eve-sentry
sudo -u eve-sentry env $(grep -v '^#' /etc/eve-sentry/eve-sentry.env | xargs) \
  .venv-server/bin/python scripts/run_server.py --esi-login-only --esi-no-browser
```

终端会打印授权 URL。在本地浏览器打开该 URL，EVE SSO 会回调到
`http://127.0.0.1:8766/callback`，再通过 SSH 隧道转发到服务器上的登录进程。
成功后重启服务:

```bash
sudo systemctl restart eve-sentry
curl http://127.0.0.1:8765/api/v1/esi/status
```

`authenticated` 为 `true` 后，`GET /api/v1/esi/session?location=true&contacts=true`
会返回当前位置和 contacts/standings 快照。不要提交 `esi_tokens.json`。

如果回调地址直接使用公网入口，例如
`http://YOUR_SERVER:8766/callback`，EVE Developers 里的 Callback URL 和
`EVE_SENTRY_SERVER_ESI_REDIRECT_URI` 必须完全一致。登录进程会在服务器本机监听
`0.0.0.0:8766`，此时需要临时放通服务器安全组或防火墙的 `8766/tcp`，授权完成后
可立即关闭该端口。

## 7. 客户端对接

服务端可访问后，把本地客户端指向 OpenResty/Nginx 统一入口:

```text
检测客户端: EVE_SENTRY_INTEL_URL=http://YOUR_SERVER
频道客户端: --server http://YOUR_SERVER
预警客户端: --server http://YOUR_SERVER
```

只有在服务器本机调试或明确放通内网访问时，才直连 `http://127.0.0.1:8765`。
公网部署应收口到 OpenResty/Nginx，不要直接把 `8765` 对全网开放。

职责边界:

- 检测客户端只上传 OCR snapshot 和可选频道日志，不查询 ESI、不做声望/敌对过滤、不生成告警。
- 服务端必须启用并配置好 ESI、友好/敌对军团联盟配置和 standing 阈值，才能完成最终敌对判断；默认中立、不良、糟糕声望都归为敌对。
- OCR active intel 代表“本地当前可见名单”，不是告警；只有服务端评分产生的 `ThreatEvent` 才会进入 `/api/v1/alerts` 和 SSE。
- 如果公网联调出现“非敌对也告警”，先看 `GET /api/v1/alerts/{id}` 的 evidence 和 `GET /api/v1/config`，不要在客户端加过滤。

## Runtime Data

常见服务端运行期文件:

- `intel.sqlite3`
- `intel_config.json`
- `esi_cache.json`
- `esi_tokens.json`
- 可选 `intel_reports.json`，用于历史 JSON 导入或迁移

## 补充说明

- `scripts/run_server.py` 会把 `EVE_SENTRY_SERVER_*` 环境变量转换成现有 `python -m app.server` 的启动参数。
- 命令行参数仍然可用，而且会覆盖环境变量拼出来的默认值。
- 服务器侧不需要安装桌面端 OCR 依赖。
