# EVE Sentry 本地联调指南

> Current status (2026-07-09): local integration should not enable or validate
> zKillboard/killboard enrichment. The detector client uploads OCR snapshots and
> optional channel log lines only; the server resolves ESI identity/standing and
> applies friendly/hostile filtering.

> Current workflow baseline (2026-07-10): 联调不验证威胁评分。服务端只查询未查询过
> ESI 的角色，并在角色被分类为敌对时触发一次性告警；中立、不良、糟糕声望统一归为敌对。

> 日期: 2026-07-01

这份文档用于在一台机器上启动服务端、检测客户端、独立频道客户端和预警客户端，验证本地威胁情报闭环。检测客户端只负责 OCR；频道日志由独立频道客户端采集。

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
- `config`: 配置文件路径、配置 schema、分类规则版本和声望/敌对规则数量。
- `esi`: 是否启用、是否已登录、token 是否过期。
- `killboard`: 禁用兼容状态；第一版不验证 killboard。
- `clients`: heartbeat 客户端数量、在线数量和最近客户端状态，包含检测端、
  预警端和频道采集器。
- `events`: alert 查询是否正常、最近 alert、SSE `/api/v1/events` 状态。

查看客户端在线状态:

```powershell
curl http://127.0.0.1:8765/api/v1/clients
```

也可以用只读联调检查脚本一次性读取服务端 health、clients、alerts、
active intel、ESI 状态、星图快照和 SSE 连通性。这个脚本只发 GET 请求，
不会创建 report、observation、alert、OCR snapshot、heartbeat，也不会 ack:

```powershell
python scripts/integration_status_check.py --server http://127.0.0.1:8765 --check-esi --check-map --check-events-stream --output .\integration-status.json
```

检测端和预警端都启动后，可加入期望条件作为验收门槛:

```powershell
python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-detector --expect-alert-client --expect-monitoring --min-targets 1 --require-event-health --check-events-stream --output .\integration-status-live.json
```

多开 EVE 联调时，把 `--min-targets` 调成实际窗口数，例如两个窗口用
`--min-targets 2`。如果没有真实 active intel，不要加 `--require-active-intel`；
真实频道或 OCR 上报产生实时情报后再使用该参数。`--min-targets` 只证明
检测端 heartbeat 里有多个窗口 target；当真实 OCR 已经识别并上报名单时，再加
`--min-active-ocr-targets 实际窗口数`，证明服务端 active OCR rows 来自多个
不同 `client_id` / `source_instance`。
`--output` 生成的 JSON 会记录检查时间、只读访问的 URL、启用的期望条件和
`write_endpoints_called: []`，可作为本次联调留证文件。证据文件还会保留
`detectors`、`alert_clients`、`channel_clients`、
`active_ocr`、`active_channel` 和 `recent_alerts`，用于确认:

- 检测端是否通过 `detector_client` heartbeat 在线。
- 多 EVE 窗口是否出现在 `details.targets`，并带有各自的 `client_id`、
  `window_title`、`region` 和 `monitoring`。
- 频道日志是否通过独立 `channel_client` heartbeat 和 `active_channel` 实时态体现。
- 预警端是否通过 `alert_client` heartbeat 在线，且 SSE 和 recent alerts
  可读取。

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
uv run python -m app.channel_client --log-dir .\samples\Chatlogs --once --include-existing --dry-run --json --all-channels
```

频道日志由独立频道客户端长驻监听并上报:

```powershell
uv run python -m app.channel_client --server http://127.0.0.1:8765 --channel "Alliance Intel"
```

`--channel` 默认按完整频道名精确匹配；未传 `--channel` 时不会扫描或上传任何
Chatlogs。需要匹配一组频道时，显式使用 `*` 或 `?` 通配符；需要独立 CLI
扫描全部频道时，必须显式加 `--all-channels`。频道 CLI 只把原始日志行提交到
`/api/v1/channel-lines`，由服务端统一解析、ESI 补全、声望敌对分类和一次性告警。

### 4. 启动检测客户端

```powershell
.\scripts\start_monitor_client.ps1 -Server http://127.0.0.1:8765
```

需要由脚本后台拉起检测端，并把 stdout/stderr 写入日志目录时:

```powershell
.\scripts\start_monitor_client.ps1 -Server http://127.0.0.1:8765 -Background -LogDir "$env:LOCALAPPDATA\EVE Sentry\logs"
```

后台启动只负责拉起检测端 GUI 并预填服务端和 Chatlogs 环境变量；真实 OCR
和多窗口监控仍需要桌面上存在实际 EVE 窗口，并在检测端里确认区域后点击
`开始监控`。没有 EVE 窗口时不会启动监控。
如果只想检查启动参数，不启动 GUI，使用:

```powershell
.\scripts\start_monitor_client.ps1 -PrintCommand -Background
```

检测客户端负责截图 OCR 并通过 OCR snapshot 只上报检测到的名单，不再采集或上传预警频道日志。默认不弹本地预警窗口，正式联调由独立预警客户端消费服务端 alert。
检测客户端不做敌对判断、不做声望过滤、不查 ESI，也不直接生成告警。OCR 名单只表示
“当前本地可见”，服务端收到后再检查该角色是否从未查询过 ESI，随后套用声望、
友好/敌对军团联盟配置和 standings 做分类。中立声望、不良声望、糟糕声望统一视为
敌对；优秀声望、良好声望视为友好。只有分类为敌对时，服务端才生成一次性
`ThreatEvent`。因此联调误报时优先检查服务端配置和 alert detail 的
`classification` / `reason`，而不是检查客户端本地过滤。
检测客户端启动后会自动向服务端上报 heartbeat，Web 面板 `Client Status`
和 `GET /api/v1/clients` 都能看到它的在线状态。旧 `GET /api/heartbeats`
仅保留给旧客户端兼容；Python 服务端不再托管旧内嵌页面。
当检测端停止监控或 heartbeat 切到 `idle` 时，服务端会把该检测端对应的
OCR realtime rows 标记为 inactive，避免旧名单继续点亮星图；历史
observations 和 alerts 仍会保留。
OCR 告警排查:

- 先查 `GET /api/v1/active-intel?source=eve-sentry-detector`，确认客户端是否只上报了当前名单。
- 再查 `GET /api/v1/alerts?limit=20` 和 `GET /api/v1/alerts/{id}`，看告警 `classification` 和 `reason` 是否命中敌对声望或敌对军团/联盟规则。
- 如果 alert 仍依赖 `score`、`min_level` 或只有 `local_ocr_seen` / 频道上下文，说明服务端仍在走旧评分模型，应优先修服务端。
- 用 `GET /api/v1/characters/by-name/{name}` 验证服务端是否已经查到角色 ID、军团和联盟。
- 用 `GET /api/v1/config` 验证友好/敌对军团联盟和 standing 阈值是否正确，默认 `hostile_standing_threshold=0.0` 表示中立及以下都算敌对。
- 误报清理只处理服务端 active intel / alert 数据；不要在客户端加入临时过滤逻辑。

多开 EVE 时，检测客户端会为当前检测到的每个 EVE 窗口启动独立监控 worker。
每个窗口使用独立 OCR `client_id`，避免一个窗口的空名单把另一个窗口仍存在的
active intel 过期掉。成员列表区域按窗口标题保存；未单独保存区域的窗口会使用
默认右侧成员列表区域。heartbeat `details.targets` 会列出每个窗口的
`client_id`、`window_title` 和 `region`，`target_count` 表示当前监控窗口数量。
监控客户端界面中的“窗口状态”表会逐行显示每个窗口的标题、区域、运行状态和
最近动作，便于确认多开时每个 worker 都在独立工作。

在没有真实 EVE 窗口时，可以先跑检测客户端 UI smoke，验证窗口能离屏创建、
主题样式已应用、状态卡布局不重叠、截图证据可生成，并确认不会触发真实截图、
OCR 识别或网络请求:

```powershell
uv run python scripts/monitor_ui_smoke.py --json --screenshot .\monitor-ui-smoke.png
```

验证窗口选择器能展示多个 EVE 窗口时，使用受控假窗口数量；这只验证 UI 和
selector 行为，不构造情报样本、不启动监控 worker:

```powershell
uv run python scripts/monitor_ui_smoke.py --json --fake-window-count 2
```

真实多开联调仍以实际 EVE 窗口为准。开始监控后，用只读检查脚本验证
`details.target_count` 和 `details.targets[]`，并把 `--min-targets` 设为实际窗口数:

```powershell
uv run python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-detector --expect-monitoring --min-targets 2 --output .\integration-status-multibox.json
```

当两个窗口都已经 OCR 到真实成员名单，并且服务端 `/api/v1/active-intel`
出现对应 realtime rows 后，再增加 active OCR target 门槛:

```powershell
uv run python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-detector --expect-monitoring --min-targets 2 --min-active-ocr-targets 2 --output .\integration-status-multibox-active.json
```

常用环境变量:

- PowerShell 启动脚本会把 `-Server`、`-ChatlogDir`、`-System`
  等参数转换成对应环境变量，再启动 `app.detector_client`。
- `EVE_SENTRY_SYSTEM=Tama`: 手工指定当前星系。
- `EVE_SENTRY_USE_ESI_LOCATION=0`: 关闭从服务端 ESI session 同步当前位置。
- `EVE_SENTRY_HEARTBEAT_INTERVAL=15`: 调整检测端 heartbeat 上报间隔，最小 5 秒。
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

### 真实验收记录模板

以下记录只用于真实 Windows / EVE / 服务端联调。不要用 sample chatlog、
fake window 或手写 POST 情报来填写“真实”结果；这些受控输入只能证明解析和
脚本行为。

建议每轮联调保存一个目录，例如 `.\evidence\2026-07-07-live\`，至少保留:

- 服务端地址: `http://127.0.0.1:8765` 或公网地址。
- 检测端启动命令: 记录 `-Server`、`-ChatlogDir`、`-System`
  和是否启用 ESI 当前星系。
- 真实 Chatlogs: 记录频道名、实际文件名、编码如果可见、`channel_offsets.json`
  或自定义 offset state 路径。
- 真实 EVE 窗口: 记录实际窗口数、窗口标题、成员列表区域截图。
- 多窗口只读证据: 保存
  `integration_status_check.py --expect-detector --expect-monitoring --min-targets 实际窗口数`
  的 `--output` JSON，检查 `summary.detector_target_count`、
  `detectors[].details.target_count` 和 `detectors[].details.targets[]`。
- 多窗口 OCR 实时证据: 当真实 OCR 已有识别结果时，保存带
  `--min-active-ocr-targets 实际窗口数` 的 `--output` JSON，检查
  `summary.active_ocr_target_count` 和 `active_ocr[].metadata.client_id`。
- 频道客户端证据: 保存带 `--expect-channel-client` 的 `--output` JSON，
  检查独立 `channel_client` heartbeat 和 `active_channel`。
- 实时情报证据: 只有真实 OCR 或真实频道日志产生情报后，才使用
  `--require-active-intel`，并检查 `active_ocr` 或 `active_channel`。
- 预警客户端证据: 保存带
  `--expect-alert-client --expect-alert-mode events --check-events-stream --check-alert-detail`
  的 `--output` JSON。预警客户端只接收服务端 alert，不执行 ack；如果有真实
  alert，记录 alert id、是否收到托盘/浮窗/声音，以及详情接口是否可读。没有最近
  真实 alert 时，`--check-alert-detail` 会记录 `skipped:no recent alerts`，
  不会创建测试 alert。

推荐命令:

```powershell
mkdir .\evidence\live
python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-detector --expect-monitoring --min-targets 2 --min-active-ocr-targets 2 --expect-channel-client --output .\evidence\live\detector-channel.json
python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-alert-client --expect-alert-mode events --check-events-stream --check-alert-detail --output .\evidence\live\alert-client.json
```

### 5. 启动预警客户端

```powershell
.\scripts\start_alert_client.ps1 -Server http://127.0.0.1:8765
```

脚本默认启动托盘后台和桌面半透明浮窗，并把预警客户端 state 放到用户
LocalAppData 下。客户端只订阅 `/api/v1/events`，只消费 `alert` 事件，
不做 ESI、分类、评分过滤或服务端 ack。

常用联调命令:

```powershell
.\scripts\start_alert_client.ps1 -Server http://127.0.0.1:8765
.\scripts\start_alert_client.ps1 -Server http://127.0.0.1:8765 -Hidden
.\scripts\start_alert_client.ps1 -Server http://127.0.0.1:8765 -State "$env:LOCALAPPDATA\EVE Sentry\alert_client_state.json"
```

真实联调推荐用后台长驻模式，便于同时观察 Web 工作台和检测端:

```powershell
.\scripts\start_alert_client.ps1 -Server http://127.0.0.1:8765 -Background -LogDir "$env:LOCALAPPDATA\EVE Sentry\logs"
python scripts/integration_status_check.py --server http://127.0.0.1:8765 --expect-alert-client --expect-alert-mode events --check-events-stream --check-alert-detail
```

预警客户端没有 `-Once`、`-Poll`、`-Ack`、`-MinLevel`、`-MinScore`
这类旧参数；在线验收以 heartbeat 和 SSE 连通性为准。

只验证启动脚本参数映射、不连接服务端、不消费真实告警时，用 `-PrintCommand`:

```powershell
.\scripts\start_alert_client.ps1 -PrintCommand -Server http://127.0.0.1:8765 -Hidden
```

浮窗只显示核心态势，例如 `S-KSWL  敌:9`。没有真实告警时只显示连接状态，不构造展示数据。

### 一键生成现场证据包

真实联调时推荐最后跑一次只读证据包脚本。它只是连续读取服务端 GET
接口并写本地 JSON 文件，不启动客户端、不提交 OCR、不上传频道样本、不 ack
告警:

```powershell
python scripts/live_acceptance_bundle.py --server http://127.0.0.1:8765 --output-dir .\evidence\live --expect-detector --expect-monitoring --min-targets 2 --min-active-ocr-targets 2 --expect-channel-client --expect-alert-client --expect-alert-mode events --check-events-stream --check-alert-detail --check-esi --check-map
```

脚本会创建带时间戳的子目录，写入:

- `baseline.json`: 服务端 health、clients、active intel、可选 ESI / map / SSE 基线。
- `detector-channel.json`: 检测端在线、监控中、多窗口 target 数、频道监控状态。
- `alert-client.json`: 预警客户端在线、SSE 连通性，以及 heartbeat 中的
  events、overlay、popup 配置状态；有最近真实 alert 时还会读取
  `GET /api/v1/alerts/{id}` 详情。
- `manifest.json`: 汇总每个检查文件、期望条件和 `write_endpoints_called: []`。

如果现场没有真实 active intel，不要加 `--require-active-intel`。只有真实 OCR
名单或真实频道日志已经进入服务端后，才把这个参数加入证据包命令。

## Runtime Data

以下文件属于本地运行状态，不应提交:

- `intel.sqlite3`: 默认 SQLite 情报数据库，也包含持久化的 client heartbeat 状态。
- `intel_reports.json`: 旧 JSON 情报数据，主要用于兼容或迁移。
- `intel_config.json`: 本地分类/告警配置。
- `esi_tokens.json`: ESI SSO token。
- `esi_cache.json`: ESI 公开资料缓存。
- `channel_offsets.json`: chatlog 采集偏移。
- `alert_client_state.json`: 预警客户端已处理 alert 状态。

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
优先检查 `alert_client_state.json` 是否已记录该 alert id、SSE 是否连通，以及
托盘进程 heartbeat 的 `last_error`。
