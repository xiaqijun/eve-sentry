# EVE Sentry 服务端部署指南

> 日期: 2026-07-01

这份文档只覆盖情报服务端部署。桌面 OCR 检测端、频道采集器和预警客户端仍然运行在本地机器上，通过 HTTP 连接远端服务端。

## 部署内容

- `app.server`: HTTP API、SSE 事件流、Web 面板
- SQLite 存储和运行期数据文件
- 可选 ESI 补全
- 可选 zKillboard 补全

当前服务端路径不依赖 `requirements.txt` 里的 GUI、OCR 和 Windows 抓图依赖，服务器上使用 `requirements-server.txt` 即可。

## 推荐目录布局

```text
/opt/eve-sentry/                  仓库目录
/opt/eve-sentry/.venv-server/     服务端虚拟环境
/etc/eve-sentry/eve-sentry.env    服务端环境变量
/var/lib/eve-sentry/              sqlite 和 runtime data
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
- `EVE_SENTRY_SERVER_DB`
- `EVE_SENTRY_SERVER_CONFIG`
- `EVE_SENTRY_SERVER_MAP_SOURCE`
- `EVE_SENTRY_SERVER_MAP_SDE_PATH`
- `EVE_SENTRY_SERVER_MAP_REGION_IDS`
- `EVE_SENTRY_SERVER_ENABLE_ESI`
- `EVE_SENTRY_SERVER_ENABLE_KILLBOARD`

如果地图使用官方 SDE，先在服务器上同步一次 YAML 地图表，再启动服务:

```bash
cd /opt/eve-sentry
sudo -u eve-sentry .venv-server/bin/python scripts/sync_sde.py \
  --build 3417089 \
  --target /var/lib/eve-sentry/sde
```

`Tenal` 的官方 region id 是 `10000045`，可直接写到
`EVE_SENTRY_SERVER_MAP_REGION_IDS=10000045`。

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

## 5. 配置 Nginx 统一入口

生产环境推荐由 Nginx 统一对外暴露 React 工作台和 Python API：

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

安装并启用 Nginx：

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

`deploy/linux/eve-sentry.nginx.conf` 默认会把：

- `root` 指向 `/var/www/eve-sentry/frontend`
- `/api/v1/events` 关闭 buffering，保证 SSE 实时推送；旧 `/api/events` 仅用于兼容旧客户端
- 其他 `/api/` 请求反代到本地 intel server
- 其余路径回退到 `index.html`，支持 React Router SPA 路由

如果服务已经直接对外暴露 `8765`，切换完成后建议在安全组或防火墙层收口，只保留 Nginx 对外入口。

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

服务端可访问后，把本地客户端指向远端地址:

```text
检测客户端: EVE_SENTRY_INTEL_URL=http://YOUR_SERVER:8765
频道客户端: --server http://YOUR_SERVER:8765
预警客户端: --server http://YOUR_SERVER:8765
```

如果服务暴露到公网，建议放到反向代理和网络访问控制后面，不要直接把 `8765` 对全网开放。

## Runtime Data

常见服务端运行期文件:

- `intel.sqlite3`
- `intel_config.json`
- `esi_cache.json`
- `esi_tokens.json`
- `zkill_cache.json`
- 可选 `intel_reports.json`，用于历史 JSON 导入或迁移

## 补充说明

- `scripts/run_server.py` 会把 `EVE_SENTRY_SERVER_*` 环境变量转换成现有 `python -m app.server` 的启动参数。
- 命令行参数仍然可用，而且会覆盖环境变量拼出来的默认值。
- 服务器侧不需要安装桌面端 OCR 依赖。
