# EVE Sentry 本地联调指南

> 日期: 2026-07-01

这份文档用于在一台机器上启动服务端、检测客户端和预警客户端，验证本地威胁情报闭环。检测客户端内置可选频道日志监控；独立频道采集器仅用于调试或批处理。

## 启动顺序

### 1. 启动服务端

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765 --db intel.sqlite3 --config intel_config.json
```

启用 ESI 公开解析和缓存:

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765 --enable-esi --esi-cache esi_cache.json --db intel.sqlite3 --config intel_config.json
```

首次使用 authenticated ESI 前先登录:

```powershell
uv run python -m app.server --esi-login-only --esi-client-id YOUR_EVE_APP_CLIENT_ID --esi-token-file esi_tokens.json --esi-token-storage auto
```

### 2. 检查服务端健康状态

```powershell
curl http://127.0.0.1:8765/api/health
```

`GET /api/health` 返回 `health.v1`，包括:

- `storage`: store 类型、SQLite/JSON 路径、是否可写。
- `config`: 配置文件路径、配置 schema、评分规则版本和 evidence 规则数量。
- `esi`: 是否启用、是否已登录、token 是否过期。
- `killboard`: 是否启用、client 类型和缓存类型。
- `clients`: heartbeat 客户端数量、在线数量和最近客户端状态，包含检测端、
  预警端和频道采集器。
- `events`: alert 查询是否正常、最近 alert、SSE `/api/v1/events` 状态。

查看客户端在线状态:

```powershell
curl http://127.0.0.1:8765/api/v1/clients
```

检测客户端启动后会以 `detector_client` 身份出现在这里；未开始监控时通常为
`idle`，点击 `Start Monitor` 后会切到 `running`。返回结果里的 `summary`
会额外汇总 `online_count`、`stale_count`、`by_type` 和 `by_status`，便于快速判断
是哪个客户端类型掉线，还是只是处于 `idle`。服务端使用 SQLite 时，这些 heartbeat
也会写入 `intel.sqlite3`，服务重启后仍能保留最近一次客户端状态，直到它们变成 stale。
客户端 `details` 里还会带 `mode`、`last_action` 和可选的 `last_error`，便于判断
当前是在 `events`/`poll`/`server_parse` 哪种模式，以及最近一次成功或失败发生在什么动作上。
现在还会带 `client_version`、`host` 和 `last_success_at`，便于区分不同机器上的
客户端实例，以及判断某个客户端最后一次成功工作是什么时候。

### 3. 验证频道解析

先用样例 chatlog 做一次 smoke:

```powershell
uv run python scripts/channel_smoke.py --json
```

只解析并打印样例，不连接服务端:

```powershell
uv run python -m app.channel_client --log-dir .\samples\Chatlogs --once --include-existing --dry-run --json
```

独立长驻监听仅用于调试；正式监控客户端会在选择频道后自动监控并上报:

```powershell
uv run python -m app.channel_client --server http://127.0.0.1:8765 --channel "Alliance Intel" --server-parse
```

### 4. 启动检测客户端

```powershell
uv run python -m app.detector_client --server http://127.0.0.1:8765
```

检测客户端负责截图、OCR 和 observation 上报；选择预警频道后，也会自动监控对应 Chatlogs 新日志并上报。未选择频道时不会提交频道日志情报。默认不弹本地预警窗口，正式联调由独立预警客户端消费服务端 alert。
检测客户端启动后会自动向服务端上报 heartbeat，Web 面板 `Client Status`
和 `GET /api/v1/clients` 都能看到它的在线状态。旧 `GET /api/heartbeats`
仍保留给旧页面和旧客户端兼容。

多开 EVE 时，检测客户端会为当前检测到的每个 EVE 窗口启动独立监控 worker。
每个窗口使用独立 OCR `client_id`，避免一个窗口的空名单把另一个窗口仍存在的
active intel 过期掉。成员列表区域按窗口标题保存；未单独保存区域的窗口会使用
默认右侧成员列表区域。heartbeat `details.targets` 会列出每个窗口的
`client_id`、`window_title` 和 `region`，`target_count` 表示当前监控窗口数量。

常用环境变量:

- `EVE_SENTRY_SYSTEM=Tama`: 手工指定当前星系。
- `EVE_SENTRY_USE_ESI_LOCATION=0`: 关闭从服务端 ESI session 同步当前位置。
- `EVE_SENTRY_HEARTBEAT_INTERVAL=15`: 调整检测端 heartbeat 上报间隔，最小 5 秒。
- `EVE_SENTRY_CHANNEL=wc.Venal+Br+Te`: 启动时预填并监控指定预警频道；为空则不提交频道日志。
- `EVE_SENTRY_CHATLOG_DIR=%USERPROFILE%\Documents\EVE\logs\Chatlogs`: 指定 EVE Chatlogs 目录。
- 检测端固定为 report-only，不再读取 `EVE_SENTRY_SHOW_POPUPS`；弹窗和声音由预警客户端负责。

### Active Intel 验证

使用受控本地请求验证实时态，不要编造 live intel。向 `/api/v1/ocr/snapshot`
POST 一份只包含 `names` 的 OCR 快照，例如同一 `client_id`、窗口和星系下的
`["Alice"]`。在 6 秒 grace period 内再次提交相同快照，随后查询
`/api/v1/active-intel`，应仍然每个 pilot 一行，并看到对应行的 `seen_count`
增长。

再在 grace period 之后提交同一上下文但 `names: []` 的空快照。默认 active list
中该 pilot 应消失；通过 observations 查询仍应能看到之前创建的历史记录。这个验证
确认客户端只上传当前检测到的名单，服务端负责 refresh/create/expire，而审计历史
不随实时态清除。

### 5. 启动预警客户端

```powershell
uv run python -m app.alert_client --server http://127.0.0.1:8765 --details --state alert_client_state.json
```

常用联调命令:

```powershell
uv run python -m app.alert_client --server http://127.0.0.1:8765 --once --include-existing --json --poll --details
uv run python -m app.alert_client --server http://127.0.0.1:8765 --popup --details
uv run python -m app.alert_client --server http://127.0.0.1:8765 --ack --ack-by alert-client
```

## Runtime Data

以下文件属于本地运行状态，不应提交:

- `intel.sqlite3`: 默认 SQLite 情报数据库，也包含持久化的 client heartbeat 状态。
- `intel_reports.json`: 旧 JSON 情报数据，主要用于兼容或迁移。
- `intel_config.json`: 本地评分配置。
- `esi_tokens.json`: ESI SSO token。
- `esi_cache.json`: ESI 公开资料缓存。
- `zkill_cache.json`: zKillboard 查询缓存。
- `channel_offsets.json`: chatlog 采集偏移。
- `alert_client_state.json`: 预警客户端已处理 alert 状态。
- `whitelist.json`: 旧本地白名单状态。

## 排查入口

- 服务端是否正常: `GET /api/health`。
- 客户端是否在线: `GET /api/v1/clients` 或 Web 面板 `Client Status`。
- ESI 是否登录: `GET /api/v1/esi/status`。
- ESI session 快照: `GET /api/v1/esi/session?location=true&contacts=true`。
- 当前 alert: `GET /api/v1/alerts?limit=20`。
- 单条详情: `GET /api/v1/alerts/{id}`。
- 事件流: `GET /api/v1/events?timeout=10&heartbeat=5`。
- 配置: `GET /api/v1/config`。

兼容说明: 服务端仍保留旧 `/api/alerts`、`/api/events` 路由用于旧客户端过渡；新前端和预警客户端文档统一以 `/api/v1/alerts`、`/api/v1/events` 为准。

如果截图区域不准，先重新选择检测客户端的成员列表区域；如果 Web 面板里看不到
`detector_client`，优先检查检测端是否连到了正确的 `--server` 地址，以及
`EVE_SENTRY_PUBLISH_INTEL` 是否被关闭；如果服务端已有 alert 但预警客户端不响，
优先检查 `alert_client_state.json`、`--include-existing`、`--unacknowledged-only`
和 `--min-level` 过滤条件。
