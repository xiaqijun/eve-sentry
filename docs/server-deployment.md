# 服务端部署

人员缓存实现和验收见[全量人员档案与分级刷新](personnel-cache-plan.md)。

### 人员档案灰度发布

管理员可在 **系统管理 → ESI 网关观测 → 全量人员档案** 保存四项配置：

| 配置 | 默认值 | 生效方式 |
| --- | --- | --- |
| 人员档案模式 | off | 下次服务端重启生效 |
| 闲时资料刷新 | 开启 | 当前实例下个调度周期采用；在途任务自然完成 |
| 历史回填 | 开启 | 当前实例下个调度周期采用；暂停保留进度 |
| 后台并发上限 | 4 | 1～4；按负载自适应，不改变两个实时槽 |

网页保存到 PostgreSQL `auth_settings` 的 `personnel_settings`，与审计记录同事务提交。
保存的配置优先于环境默认值；只有尚未保存时才使用 `EVE_SENTRY_PERSONNEL_CACHE`（默认 `off`）。
页面分别显示“当前运行”和“已保存”，不会因保存模式而自动重启或中断 SSE。请在受控部署/维护
窗口重启，并重新读取页面确认两者一致。默认 off 不创建人员档案表或启动档案线程；配置仍可管理。

模式含义：

| 值 | 行为 |
| --- | --- |
| off | 原解析链路；不创建人员表、不启动档案线程；回滚选项 |
| shadow | PostgreSQL 档案写入、后台刷新和回填；消费者仍使用原解析器 |
| on | OCR 缓存优先读取档案、异步刷新、有效观察重分类、人员名单更正 |

`shadow/on` 要求 PostgreSQL 和启用 ESI，否则启动报错，不能静默宣称启用成功。
启动时幂等创建新增人员表及索引，不删除或改写旧上报历史；使用独立 1～8 连接池，
保留 DSN 的 search_path 等选项，连接等待 2 秒、SQL 超时 5 秒、锁等待 2 秒。
归档数据库暂时失败时保留内存成功资料并重试，观测状态显示 degraded；冷 miss 不阻塞视觉状态。

发布顺序：受保护工作流发布 Gateway（归属 TTL 配置改为 300 秒）→ 更新机器人空名单更正支持
→ 服务端 `shadow` 验证 → 同一受控服务实例 `on` 灰度。当前开关是服务实例级，不提供逐节点开关。
检查管理员 Gateway 页的档案量、积压、失败/迟到计数；实测缓存命中、首次识别、敌我改判、清空和
重连重放的端到端延迟。未经这些生产验收，不把本地测试称为上线完成。

空闲调度器自动按 100 条分页导入旧缓存中已确认姓名和持久上报中的可信人员资料，进度保存在
`personnel_backfill_progress`。只导入身份及真实出现时间，不伪造归属获取时间；旧缓存已清除的数据
只能从可信历史恢复。也可在受控运维环境使用 `python -m scripts.backfill_personnel --help`，
通过 `--dsn-env EVE_SENTRY_POSTGRES_DSN` 指定保存 DSN 的环境变量名，并用 `--max-batches` 限量。
命令行和日志不要放生产密码。回滚时在后台保存 `off` 后受控重启；有持久配置时仅修改环境变量
不会覆盖它。保留档案表、任务和回填进度，禁止删表回滚。

“闲时资料刷新”只暂停非实时公共资料刷新，不停止当前人员解析、归属刷新或联系人校验；历史
回填另有独立开关。关闭档案模式时这些调度设置仅保存，不运行。开启档案模式前仍需要 PostgreSQL
和 ESI。当前采用单个服务实例部署，调度热应用只作用于接收保存请求的实例，不承诺多实例广播。
多实例部署必须先设计配置分发；不要将此接口接入未提供分发能力的随机负载均衡管理入口。

当前生产结构：

```text
OpenResty/Nginx :80/:443
  -> frontend/dist
  -> /api/ -> Python 127.0.0.1:8765
Python app.server (114.132.167.239)
  -> PostgreSQL
  -> public ESI (local or private Gateway)
  -> SDE

Optional public ESI Gateway (47.243.104.165)
  -> ESI / cache / rate limit
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

升级到包含视觉波次峰值和波次人员快照的版本后，启动迁移会为 `hostile_waves` 自动增加
`peak_hostile_count`、`personnel_json`。当前仍活跃的波次会在启动协调时用 active intel 回填峰值
和已解析人员；已经关闭
的旧波次无法从现有数据反推出历史红色图标数量，只有能关联到已验证人员告警的旧波次会
继续参与报表统计。升级不需要手工执行 SQL。

连接池默认最小 2、最大 8 个连接。不要为每个 SSE 心跳重新创建 PostgreSQL 连接。
服务启动读取客户端心跳时，会按“用户 + 客户端类型 + 主机”保留最新实例并删除更旧的
重复实例；缺少用户或主机归属的记录不会自动删除，避免误清理不同设备。
迁移会为 `system_id` 和角色 ID JSON 建立查询索引。历史列表默认最多返回 100 条，最大
1000 条；批量导出或后台巡检应使用 `/api/v1/reports`、`/api/v1/observations` 的游标分页，
不要通过省略 `limit` 请求完整历史。

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
EVE_SENTRY_SERVER_KEY_RISK_CONTROL=on
EVE_SENTRY_SERVER_MAP_SOURCE=sde
EVE_SENTRY_SERVER_MAP_SDE_PATH=/var/lib/eve-sentry/sde/BUILD_NUMBER
EVE_SENTRY_SERVER_MAP_REGION_IDS=10000045
EVE_SENTRY_SERVER_ENABLE_ESI=1
EVE_SENTRY_SERVER_ESI_BACKEND=local
EVE_SENTRY_SERVER_ESI_GATEWAY_URL=http://10.233.53.17:8787
EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN=
EVE_SENTRY_SERVER_ESI_REMOTE_TIMEOUT=8
EVE_SENTRY_SERVER_ESI_NO_LOCAL_FALLBACK=0
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

`EVE_SENTRY_SERVER_HOT_REPORT_LIMIT` 只控制告警评分使用的内存热集合，不是 HTTP 历史
查询的分页大小。预警 SSE 的 `alert` 事件仅对活跃情报引用的报告生成；`bootstrap.map.systems`
同时包含红色图标的即时 Presence 状态，因此无需等待 OCR 报告。兼容 `/api/events` 也会在达到
单次 `limit` 后停止评分。部署后可用访问日志的耗时字段监控 `/api/v1/events` 首帧和历史
列表延迟。

`EVE_SENTRY_SERVER_KEY_RISK_CONTROL` 默认为 `on`，设备密钥会经过 Listener、公共 ESI、
允许军团和角色白名单校验。设为 `off` 后，有效设备密钥直接获得客户端权限，管理员可为
未登录 EVE SSO 的用户签发密钥；身份检查接口直接确认并跳过 ESI。管理员也可以在 Web 的
“系统管理 → 安全设置”中切换，Web 保存的值会写入 PostgreSQL 并覆盖启动默认值。该开关不
关闭密钥认证、账号禁用、吊销或只读密钥权限限制。

### 公共 ESI Gateway

公共角色、军团、联盟和星系解析可以通过独立 Gateway 访问 `47.243.104.165`。当前
Gateway 使用 ZeroTier 地址 `10.233.53.17:8787`，只允许 114 的 `10.233.53.204`
调用，并在自身缓存和限流；114 仍保留本地 ESI 回退。部署模板为：

```text
esi-gateway/deploy/linux/eve-sentry-esi-gateway.service
esi-gateway/deploy/linux/eve-sentry-esi-gateway.env.example
```

Gateway 不处理 EVE SSO、OAuth token、角色当前位置或联系人 standings。启用前先从 114
验证：

```bash
curl http://10.233.53.17:8787/health
curl -H "Authorization: Bearer $EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN" \
  http://10.233.53.17:8787/v1/systems/30000142
```

确认健康后，将 `EVE_SENTRY_SERVER_ESI_BACKEND` 改为 `remote` 并重启 114 服务。出现
代理故障时改回 `local`；保留 `EVE_SENTRY_SERVER_ESI_NO_LOCAL_FALLBACK=0`，保证公共解析
失败不会阻塞 OCR、心跳和告警。

管理员可在 Web 的“系统管理 → ESI 网关观测”（`/admin/esi-gateway`）查看 Gateway
健康摘要和 114 远端客户端请求指标。页面调用 114 的
`GET /api/v1/admin/esi-gateway`，不会把 Gateway Bearer token 暴露给浏览器。若页面显示
“网关不可达”，先检查 114 到 `10.233.53.17:8787` 的 ZeroTier 路由和
`eve-sentry-esi-gateway.service` 日志。

Gateway 观测中的 `requests` 是进入网关的总请求数，`upstream_requests` 是实际访问 ESI
的请求数，`cache_hits`/`cache_misses` 用于计算命中率，`request_rate_per_second` 是最近
60 秒实际请求率，`endpoints` 提供按端点拆分的统计。过期缓存条目不会计入 `cache_entries`；
批量名称或 ID 的顺序变化不会制造新的缓存键。

### ESI 缓存策略

服务端名称到角色 ID 的缓存默认有效 24 小时；角色基础资料默认有效 6 小时，当前角色
军团/联盟归属通过批量 `POST /characters/affiliation` 单独缓存 1 小时，以缩短人员更换
军团/联盟后的陈旧窗口；星系静态资料仍使用 24 小时。缓存过期后，
正常请求会触发刷新；ESI 暂时不可用时，显式允许 stale 的后台流程最多使用 7 天内的旧值。

`esi_cache.json` 保存时会清理超过 7 天 stale 窗口的记录，并使用临时文件原子替换；
默认最多保留 20,000 条记录。`/api/health` 和管理员 ESI 观测中的 `evictions`、
`stale_entries` 用于发现缓存膨胀。人员白名单或分类配置发生变更时，应调用对应缓存
失效流程，不要通过延长 TTL 来规避刷新。

Gateway 默认最多保留 4,096 个响应条目，并按 key 合并并发 miss；上游失败默认负缓存
30 秒，过期成功值在 300 秒 stale 窗口内可作为 `cache: "stale"` 返回。健康响应中的
`cache_evictions`、`inflight`、`coalesced`、`negative_hits`、`negative_entries` 和
`stale_served` 用于判断容量、击穿和上游故障退化情况。Gateway 的
TTL 只控制网络层响应缓存，不能替代服务端人员资料的业务 TTL。

实时人员解析不再请求 zKillboard 战绩，也不会随公共 ESI 自动开启战绩查询。
`EVE_SENTRY_SERVER_ENABLE_ZKILL` / `EVE_SENTRY_SERVER_DISABLE_ZKILL` 已废弃，启动脚本忽略它们；
旧 CLI 参数 `--enable-killboard`、`--disable-killboard`、`--zkill-cache` 仅兼容解析，不生效。
监控服务无需为人员解析配置 zKillboard 出站访问。机器人和星图的 zKillboard 链接直接由角色 ID
生成；点击链接才由用户浏览器打开。机器人独立的手动战报分析仍按机器人部署文档配置。

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
EVE_SENTRY_SERVER_ESI_SCOPES=esi-location.read_location.v1,esi-characters.read_contacts.v1,esi-characters.read_standings.v1,esi-corporations.read_contacts.v1,esi-alliances.read_contacts.v1,esi-search.search_structures.v1
EVE_SENTRY_SERVER_ESI_STANDINGS_TTL=600
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
5. 配置 EVE SSO 和 QQ 机器人只读服务密钥；若使用机器人手动战报分析，另按机器人文档配置出站访问。
6. 升级桌面客户端并完成身份校验。
7. 切换到 `enforce`，重启服务并验证健康、登录、OCR、心跳和 SSE。

## 自动部署

`.github/workflows/deploy-server.yml` 在每次 `main` 推送后执行完整 Python 测试、前端
测试和生产构建。验证通过后，工作流上传单一部署归档并调用
`deploy/ci/deploy_release.sh`。远端流程使用部署锁，备份受管理的后端文件、前端静态
目录和 systemd 单元，安装服务端依赖并重启；就绪检查失败时自动恢复备份。默认保留
最近 5 次部署备份。

ESI Gateway 使用独立工作流 `.github/workflows/deploy-esi-gateway.yml`。它只在公共 ESI
客户端、Gateway 脚本或 Gateway systemd 单元变化时触发，上传归档到 `47.243.104.165`
并执行健康检查；健康响应必须包含 `cache_entries`，否则部署失败并自动恢复备份。该
工作流使用独立的 `EVE_SENTRY_ESI_GATEWAY_SSH_KEY`、
`EVE_SENTRY_ESI_GATEWAY_KNOWN_HOSTS` Secret，以及
`EVE_SENTRY_ESI_GATEWAY_DEPLOY_HOST/USER/PORT` Variables。

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
