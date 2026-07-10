# EVE Sentry 情报平台开发路线

> Current status (2026-07-09): zKillboard/killboard work is no longer part of
> the active first-version scope. Runtime code, scoring evidence, deployment
> environment variables, and tests have been removed or disabled. Future
> killmail intelligence should be planned again with bounded storage and memory
> behavior before implementation.

> Current workflow baseline (2026-07-10): 第一版去掉评分系统，改为服务端声望驱动
> 敌对分类和一次性告警。ESI 只查询“未查询过 ESI”的角色；中立、不良、糟糕声望统一归为敌对。具体工作流以
> `docs/intel-workflows.md` 为准。

> 日期: 2026-07-01
> 配套文档: `docs/intel-platform-architecture.md`
> 状态: 当前实现已经进入“多源情报闭环补强”阶段，不再是早期单机 OCR 规划。

## 当前完成度

已经完成并有测试覆盖的主干能力:

- 桌面检测客户端: EVE 窗口识别、区域截图、PaddleOCR 本地频道识别、后台截图、OCR snapshot 上报、检测端默认不弹窗。
- 客户端拆分: 检测客户端只负责采集和上报，独立预警客户端只负责消费服务端 alert，敌对判断统一在服务端完成。
- 统一情报模型: `Observation` 和 `ThreatEvent` 已作为服务端统一输入/输出模型。
- 服务端 API: 已支持 observation 上报、旧 `/api/intel` 兼容、alert 查询、alert detail、实体情报查询、ack、地图快照、配置更新和 health。
- 事件推送: `/api/v1/events` 已提供 SSE alert 事件流，支持过滤、游标续接、keepalive、并发订阅和轮询 fallback。
- 预警频道采集: 已有 chatlog watcher、频道解析器和 `app.channel_client`，支持 UTF-16/UTF-8、断点续读、服务端解析上报。
- ESI 公开补全: 已有 client/cache/resolver，支持名字解析、角色/星系公开资料、缓存、`not_found` negative cache 和失败降级。
- ESI SSO: 已有 PKCE 登录、token 保存/刷新、`/api/v1/esi/status`、`/api/v1/esi/session`、当前位置和 contacts/standings 快照。
- 击毁画像: 已从第一版移除，不再作为告警来源。
- 分类告警: 第一版已接入 `ClassificationEngine`，默认服务端按声望/军团/联盟规则把目标归为敌对后生成一次性告警；OCR 可见名单本身只刷新实时态。
- 存储: 默认 SQLite，保留 JSON 兼容路径，并有旧 JSON 导入脚本。
- Web 面板: 已有星图、手工情报、配置入口、服务端 alert detail 展示、实体情报摘要、ESI session 状态、客户端 heartbeat 状态和 SSE 更新；评分配置入口需要改为分类配置。

## 版本分层

### V1: 当前可用第一版

当前版本已经满足第一版定义。核心判断标准是:

- 多客户端拆分完成，服务端作为统一情报中心
- 健康检查、heartbeat、alert detail、实体情报查询、Web 状态页可用
- 默认 SQLite、SSE、轮询 fallback 和本地联调链路已跑通
- 全量测试通过，可作为继续迭代的稳定基线

V1 已纳入范围:

- 检测客户端、频道采集器、预警客户端、服务端拆分完成
- 服务端统一接收 `Observation`，统一产出 `ThreatEvent`
- 默认 SQLite 存储，保留 JSON 兼容导入路径
- `GET /api/health`、`GET /api/v1/clients`、`GET /api/v1/alerts/{id}` 和实体情报查询接口可用
- Web 面板可查看星图、手工情报、告警、详情、ESI 状态和客户端状态
- heartbeat 已支持汇总诊断、SQLite 持久化，以及 `mode`、`last_action`、`last_error`、
  `client_version`、`host`、`last_success_at`
- ESI 公开解析、`not_found` 查询缓存、SSO 会话、当前位置和 contacts/standings 已接入第一版
- zKillboard 画像和评分 evidence 已移出第一版
- 预警客户端和 Web 面板优先消费服务端解释链，而不是各自重复推断
- Active Intel realtime state: OCR snapshot diffing, channel TTL, clear-message
  deactivation, and frontend active list.
- OCR 观察和最终告警分离: 本地名单只刷新 active state，只有服务端分类为敌对后才生成一次性 alert。

V1 暂不包含:

- 用大量真实频道样本覆盖更多联盟/军团格式
- 完整的 ESI token 迁移和轮换策略
- ESI 查询缓存治理: `not_found` 已接入，`failed` / `retry_after` 和更细失败类型待补
- 更完整的客户端运行诊断字段
- 更强的 UI 产品化打磨和回放工具

### V1.1: 下一阶段增强

优先纳入:

- 基于真实频道样本继续补解析规则
- ESI token 迁移和轮换策略
- PostgreSQL 分类告警 schema 和 ESI 查询缓存表
- heartbeat 继续补 `startup_time`、`pid` 和最近一次成功 payload 摘要

### V2: 更完整的平台化能力

后续再纳入:

- 分类告警产品化、状态变化告警和查询缓存治理
- 更强的多源画像聚合视图
- 更成熟的 UI/运维体验
- 更系统化的告警与追踪工作流

最近完成的解析修复:

- 低置信度 OCR / 频道 observation 会降权，无法解析出有效目标或星系时保留 observation 但不直接生成 alert。
- 启用 ESI resolver 时，observation 会记录 `esi_resolution`，用于解释解析、修正或抑制的原因。
- OCR observation 现在只表示本地可见名单；没有被分类为敌对时，不会因为 `local_ocr_seen` 或频道上下文单独生成 alert。
- 频道行中唯一星系候选可修正 `system_name`，并重算 `names` / `hostile_count`。
- 多个星系候选不再盲目修正，会以 `ambiguous` 状态保存在 `esi_resolution`。
- 像 `Tama Oijanen` 这种由已解析星系组成的链路名会从角色候选中抑制，并记录到 `suppressed_name_candidates`。

## 待做总览

### P0: 服务端情报详情查询层

目标: 让 Web 面板拿到稳定、统一、可解释的威胁详情；预警客户端第一版只做本地提示和核心态势展示。

待做:

- 增强 `GET /api/v1/alerts/{id}` 的稳定输出契约，明确 `context` 和 `explanation` 字段版本。
- 把 ESI 解析结果、频道上下文、standing、分类结果和分类原因统一成一个可前端直接展示的详情结构。
- 增加按角色、星系、军团、联盟查询相关 observation / alert / classification 的组合接口。
- 预警客户端只消费服务端 alert 事件，不在客户端重复推断，也不维护 `--details` 输出模式。
- Web 面板详情视图复用服务端 `alert_detail.v1` 和实体情报查询接口。
- 补接口文档和回归测试，确保旧 alert detail 兼容。

验收:

- 单条 alert 能展示“谁、在哪、为什么报、证据来自哪里、哪些候选被 ESI 抑制或修正”。
- Web 面板读取服务端详情；预警客户端只显示 `星系名  敌:x` 并本地去重。
- ESI 未启用或临时失败时详情接口仍返回可用的降级解释。

### P1: 预警频道解析增强

目标: 覆盖更多联盟/军团频道格式，降低误报并保留足够回溯信息。

待做:

- 扩展文本模式: 多人混合、跳数链、方向词、缩写、中文/英文混杂、无星系但有上下文的消息。
- 对频道行建立解析诊断字段，例如命中的模式、被忽略的 token、候选星系、候选角色。
- 增加更多真实样例测试，尤其是星系链和人名歧义。
- 提供样例 chatlog smoke fixture，便于联调时快速验证解析效果。

验收:

- 常见预警频道格式能稳定产出 `intel_channel` observation。
- 歧义内容不直接制造高危 alert，而是进入 observation 和解释链。

### P1: ESI 情报画像补强

目标: 让 ESI 不只是名字解析，而是成为角色/组织/位置情报画像来源。

待做:

- 统一角色、军团、联盟、星系 profile 的缓存结构和过期策略。
- 扩展 contacts/standings 到军团和联盟维度，避免只靠角色 standing。
- 记录 ESI 查询失败、缓存命中、过期缓存 fallback 等状态，写入解释链。
- 完善 token 迁移、轮换和跨平台安全存储策略。

验收:

- alert 详情能解释“该角色为何被视为敌对/中立/未知”。
- ESI 不可用时服务端仍能上报 observation，但分类为 `unknown`，不生成敌对告警。

### P1: 击毁查询和行为画像补强

状态: 暂停并移出第一版。

目标: 后续如重新引入 killmail 情报，必须先完成有界缓存和内存上限设计。

待做:

- 不部署 zKillboard client。
- 不维护 `zkill_cache.json`。
- 不把击毁画像纳入第一版分类或告警。
- 后续在已有 killmail id/hash 时再评估 ESI killmail 权威详情。

验收:

- 第一版主报警链路不依赖 killmail 数据。

### P2: 分类告警和配置产品化

目标: 让声望驱动的敌对分类规则可理解、可调、可追踪。

待做:

- 给 `classification` 和 `reason` 增加稳定枚举。
- 让配置 API 暴露当前规则版本、默认值来源和更新时间。
- 增加分类回放测试: 同一 observation 在不同配置下生成可预测分类。
- 为友好/敌对军团联盟、standing 阈值和敌对声望映射提供更明确的 UI/接口说明。
- 建立 ESI 查询缓存表，保证只查询未查询过 ESI 的角色，并缓存 not_found / failed 状态。

验收:

- 用户能从 alert detail 看懂敌对分类如何产生。
- 调整配置后，新 alert 和事件流使用新规则，旧 alert 的历史解释不被悄悄改写。

### P2: 运行和联调体验

目标: 让服务端、检测端、频道采集器、预警端更容易一起启动和排查。

待做:

- 已补本地联调文档，说明服务端、检测端、频道采集器、预警客户端启动顺序。
- 已增加健康检查接口，暴露 ESI、SQLite、配置和 SSE/alert 查询状态；killboard 仅保留禁用兼容状态。
- 已为客户端状态文件、SQLite、ESI token/cache 补统一 runtime data 说明。
- 更新截图区域选择和后台监控说明，区分检测端 UI 和服务端预警链路。

验收:

- 新机器按文档能启动本地闭环。
- 断网、ESI 未登录、客户端重启都有明确提示。

## Backlog 明细

### INTEL-P0-01: 固化 alert detail 契约

优先级: P0

状态: 已完成第一版。服务端现在返回 `schema_version: alert_detail.v1`、
`entities`、`context.resolution` 和 `explanation.degraded_sources`，并保持旧的
`alert`、`observation`、`context`、`explanation` 字段兼容。

范围:

- 服务端: `app/server/intel_store.py` 的 `alert_detail()` 和解释生成逻辑。
- HTTP: `app/server/http_server.py` 的 `GET /api/v1/alerts/{id}`。
- 客户端: `app/intel_client.py` 的 `alert_detail()` 返回契约。
- 测试: `tests/test_http_server.py`、`tests/test_intel_client.py`。

输出字段建议:

- `schema_version`: 例如 `alert_detail.v1`。
- `alert`: 原始 alert 字段，保持向后兼容。
- `observation`: 源 observation。
- `entities`: 角色、星系、军团、联盟等稳定 ID 和展示名。
- `context`: 频道上下文、ESI profile、ESI lookup 状态、standing、suppressed candidates。
- `explanation`: `summary`、`reasons`、`context`、`sources`、`degraded_sources`。

完成标准:

- 旧客户端读取原有 `alert` / `observation` / `context` / `explanation` 不破。
- 关闭 ESI 时 `degraded_sources` 能说明降级原因；killboard 不再是第一版降级源。
- 新测试覆盖 ESI 修正、歧义候选、星系链抑制、ESI 查询失败四种场景。

### INTEL-P0-02: 重做独立预警客户端

优先级: P0

状态: 已完成第一版。`app.alert_client` 已重做为托盘后台 + 桌面半透明浮窗，
只消费 `/api/v1/events` 的 `alert` 事件，忽略 `bootstrap` / keepalive，
本地保存 seen alert id 去重，不调用服务端 ack。

范围:

- `app/alert_client.py` 的托盘、浮窗、SSE worker 和本地 state。
- `scripts/start_alert_client.ps1` 的新参数模型。
- `tests/test_intel_client.py`。

待做:

- 补更多托盘菜单和浮窗位置设置。
- 补真实 Windows 桌面长驻验收截图。
- 如后续需要详情，优先跳转 Web 工作台，不在本地客户端复制详情 UI。

完成标准:

- 预警客户端不再自行推断 ESI、分类解释或评分。
- 服务端有真实 alert 时，本地浮窗显示 `星系名  敌:x`，且不会 ack 服务端告警。

### INTEL-P0-03: 增加情报查询接口

优先级: P0

状态: 已完成第一版。服务端已提供 `GET /api/intel/character/{character_id}`、
`GET /api/intel/system/{system_id}`、`GET /api/intel/corporation/{corporation_id}`
和 `GET /api/intel/alliance/{alliance_id}`，统一返回 `intel_entity.v1`、
相关 observations、alerts、activity、counts 和 filters。

范围:

- `app/server/intel_store.py` 增加查询方法。
- `app/server/http_server.py` 增加 REST 路由。
- SQLite store 如需新增索引或查询路径，更新 `app/server/sqlite_store.py`。
- 测试放在 `tests/test_http_server.py` 和 `tests/test_intel_store.py`。

接口草案:

- `GET /api/intel/character/{character_id}`: 角色相关 observation、alert、profile、classification。
- `GET /api/intel/system/{system_id}`: 星系相关 observation、alert、classification 聚合。
- `GET /api/intel/corporation/{corporation_id}`: 军团相关 profile、alert、classification 聚合。
- `GET /api/intel/alliance/{alliance_id}`: 联盟相关 profile、alert、classification 聚合。

完成标准:

- 支持 `limit`、`since`、`acknowledged`、`classification`、`reason` 这类通用过滤。
- 无 ESI 时接口仍返回已知 observation；未分类为敌对时不生成新 alert。
- Web 面板后续可以直接用这些接口做详情页或侧栏。

### INTEL-P0-04: Web 面板复用服务端详情

优先级: P0

状态: 已完成第一版。星图 Web 面板的 alert `Details` 按钮现在优先读取
`GET /api/v1/alerts/{id}` 的 `alert_detail.v1`，展示服务端生成的
`explanation.summary`、`reasons`、`context`、`degraded_sources` 和实体列表；
同时按 detail 中的角色、星系、军团、联盟 ID 调用 `/api/intel/...` 展示关联
observation / alert / activity 摘要，不再在浏览器端分别拼角色 profile、星系
profile 和 classification。

范围:

- React 工作台的 alert detail 渲染和加载逻辑。
- 前端工作台测试中的告警详情能力断言。

完成标准:

- Web 面板和预警客户端消费同一份服务端 explanation。
- 实体详情优先走 `/api/intel/character|system|corporation|alliance/...`。
- ESI 不可用时，Web 面板展示服务端 `degraded_sources`，不自行推断。

### INTEL-P1-01: 频道解析诊断字段

优先级: P1

状态: 已完成第一版。频道 parser 会在 observation metadata 中写入
`parse_diagnostics`，包含 `parse_pattern`、`system_candidates`、
`name_candidates` 和 `ignored_tokens`；服务端 `/api/v1/channel-lines` 会保留该诊断字段。

范围:

- `app/channels/parser.py`。
- `app/server/intel_store.py` 的频道 ESI 修正和 metadata 记录。
- `tests/test_channels.py` 或现有频道解析测试。

metadata 建议:

- `parse_pattern`: 命中的解析模式。
- `system_candidates`: 频道文本里可能的星系。
- `name_candidates`: 频道文本里可能的角色名。
- `ignored_tokens`: 因关键词、数量、方向、星系链被忽略的 token。
- `suppressed_name_candidates`: 已实现，继续作为标准字段保留。

完成标准:

- 每条 `intel_channel` observation 都能解释“为什么这样解析”。
- 歧义行不触发高危 alert，但能在 alert detail 或 observation detail 里回看。

### INTEL-P1-02: 频道样例库和 smoke 验证

优先级: P1

状态: 已完成第一版。`samples/Chatlogs/Alliance Intel_20260630_120000.txt`
已经覆盖数量预警、位置短语、方向/跳数、星系链和低置信度 raw 行；`scripts/channel_smoke.py`
测试会验证样例能生成 observation、alert 和解析诊断字段。

范围:

- `samples/Chatlogs/` 或 `tests/fixtures/chatlogs/`。
- `scripts/channel_smoke.py`。
- 频道解析和服务端端到端测试。

样例类型:

- `Tama +3 reds`
- `Oijanen Some Pilot`
- `Tama Oijanen` 星系链。
- 中英文混合、多人混合、无星系、只有方向、重复上报。

完成标准:

- 一条命令能验证样例 chatlog 是否生成预期 observation / alert。
- 样例库不会包含真实玩家隐私数据。

### INTEL-P1-03: ESI profile/cache 状态入解释链

优先级: P1

状态: 已完成第一版。ESI cache 会记录 `fetched_at` / `expires_at`，resolver 返回
profile 时会附带 `cache_status`（例如 `refreshed`、`cached`、`stale`），alert
detail 的 profile context 会把 cache 状态写入 explanation。

范围:

- `app/esi/cache.py`、`app/esi/resolver.py`。
- `app/intel/enrichment.py`。
- `app/server/intel_store.py` 的 alert context。
- `tests/test_esi_resolver.py`、`tests/test_intel_enrichment.py`、`tests/test_http_server.py`。

待做:

- 给 profile / resolver 结果增加 `cache_status`、`fetched_at`、`expires_at` 或等价字段。
- 记录 ESI failure 类型: disabled、unauthorized、not_found、rate_limited、network_error。
- alert detail 的 `degraded_sources` 包含 ESI 降级原因。

完成标准:

- 用户能知道 ESI 资料是新鲜缓存、过期缓存，还是根本没查到。
- ESI 失败不影响 observation 保存；分类为 `unknown`，不触发敌对告警。

### INTEL-P1-04: zKillboard 查询状态和聚合解释

优先级: P1

状态: 已从第一版撤回。早期 zKillboard JSON cache 会导致服务端内存持续增长，
相关 runtime client、analyzer、scoring evidence、部署环境变量和测试已删除或禁用。
后续如恢复击毁画像，需要重新设计有界缓存、持久化结构、限速、退避和内存上限。

范围:

- 当前保留禁用兼容路由 `GET /api/v1/kill-activity/...`，返回 404。
- `app/intel/enrichment.py` 不再收集 kill activity。
- `app/intel/scoring.py` 不再生成 killboard evidence。

待做:

- 本项暂停；不要继续实现 kill activity 字段。
- 明确角色、星系、军团、联盟各自 TTL 和失败 fallback。
- 进一步细化退避策略配置，例如不同 HTTP 状态码、不同 scope 的退避时长。

完成标准:

- 本项已撤回；击毁画像不再进入第一版 alert detail。

### INTEL-P2-01: 分类规则版本化

优先级: P2

状态: 需要按新工作流重做。旧 `scoring_version`、`Evidence.rule_id` 和评分回放属于
兼容历史；第一版主线应改为 `classification_version`、`classification`、`reason`
和分类回放测试。

范围:

- 新 `app/intel/classification.py`。
- `app/core/models.py` 的 alert / classification 序列化。
- `docs/intel-config-api.md`。
- 新分类测试。

待做:

- 梳理 `classification` / `reason` 枚举。
- 在 alert 或 explanation 中记录 `classification_version`。
- 配置 API 返回分类规则版本和默认值来源。
- 增加分类回放测试，固定同一 observation 在不同 config 下的输出。

完成标准:

- 用户能从 alert detail 看到敌对由哪条规则命中。
- 未来调整分类规则时，可以区分历史 alert 和新规则 alert。

### INTEL-P2-02: 本地联调和健康检查

优先级: P2

状态: 已完成增强版。新增 `GET /api/health`，返回 `health.v1` 的
storage/config/ESI/clients/events 状态；新增 `GET /api/v1/clients`
和 Web 面板 `Client Status` 区块，用于显示检测端、预警端、频道采集器等客户端在线状态；
检测端 GUI 现已上报 `detector_client` heartbeat，并在开始/停止监控时同步刷新状态；
`/api/v1/clients` 现已补充 `summary.by_type`、`summary.by_status` 和 `stale_count`
等汇总诊断字段，Web 面板会直接消费这些状态摘要；默认 SQLite store 现已把
`client_heartbeats` 持久化到 `intel.sqlite3`，服务重启后可保留最近一次客户端状态；
客户端 heartbeat `details` 现已补充 `mode`、`last_action` 和 `last_error`，
用于定位当前运行模式、最近一次成功动作和最近一次失败摘要；现已进一步补充
`client_version`、`host` 和 `last_success_at`，用于区分客户端构建、运行宿主和
最近一次成功时间；
`docs/local-integration.md` 已记录本地启动顺序、健康检查、runtime data 和排查入口。

范围:

- 新增或更新运行文档。
- `GET /api/health`。
- 服务端、检测端、频道采集器、预警端启动命令。

健康检查建议:

- `storage`: SQLite 是否可写、当前 db 路径。
- `esi`: 是否启用、是否登录、token 是否过期。
- `killboard`: 禁用兼容状态。
- `events`: SSE 是否可用、最近 alert 时间。
- `config`: 配置文件路径和加载状态。

完成标准:

- 新机器按文档能启动服务端、检测端、频道采集器、预警客户端。
- 遇到 ESI 未登录、截图区域没选好时有明确排查入口。

## 已完成阶段归档

### 阶段 1: 统一情报模型

状态: 已完成主干。

- `app/core/models.py` 提供 `Observation` / `ThreatEvent`。
- 服务端支持 `POST /api/observations`，保留 `/api/intel` 兼容。
- 预警客户端消费 alert，不再直接理解旧 report。

### 阶段 2: 预警频道解析

状态: 已完成第一版，进入格式增强阶段。

- `app/channels/log_watcher.py`
- `app/channels/parser.py`
- `app/channel_client.py`
- `POST /api/v1/channel-lines`

### 阶段 3: ESI 公开数据补全

状态: 已完成第一版，进入画像补强阶段。

- `app/esi/client.py`
- `app/esi/resolver.py`
- `app/esi/cache.py`
- 角色、星系解析和缓存。
- 频道星系修正、歧义候选保存、星系链人名候选抑制。

### 阶段 4: 击毁查询和行为画像

状态: 暂停并移出第一版。

- 不部署 zKillboard client。
- 不维护 `zkill_cache.json`。
- 不把击毁画像纳入当前分类或告警。

### 阶段 5: 分类告警

状态: 旧实现待替换。第一版主线改为分类告警。

- 计划新增 `app/intel/classification.py`。
- 敌对/友好、友军/敌对军团联盟、standing 分类。
- alert detail 应包含 classification、reason、context 和 explanation。

### 阶段 6: ESI SSO

状态: 已完成本地会话主干。

- `app.esi.sso` 本地 PKCE 登录。
- `app.esi.session` token refresh/session snapshot。
- `/api/v1/esi/status` 和 `/api/v1/esi/session`。
- 检测端可从服务端 ESI session 同步当前位置。
- Web 面板可展示 ESI session 状态和当前位置。

### 阶段 7: SQLite 和事件推送

状态: 已完成主干。

- 默认 SQLite store。
- JSON 兼容和导入脚本。
- `/api/v1/events` SSE 事件流。
- Web 面板和独立预警客户端都支持 SSE；独立预警客户端不保留轮询 fallback。
- 已覆盖并发订阅、重连续接、重启后恢复等测试。

兼容说明: 旧 `/api/alerts`、`/api/events` 路由仍保留给旧客户端过渡；新前端和客户端默认使用 `/api/v1/alerts`、`/api/v1/events`。

## 当前建议开发顺序

1. 等待真实预警频道样本后继续补解析规则；没有真实样本时不构造频道数据作为测试依据。
2. 部署并联调 PostgreSQL 第一版存储入口，验证 observations、active_intel、ack 和 heartbeat 持久化。
3. 继续细化 PostgreSQL schema，拆出 ESI 查询缓存、classification history 和配置表。
4. 设计 ESI token 迁移策略。
5. 继续补 heartbeat 诊断字段，例如启动时间、进程标识和最近一次成功 payload 摘要。

## 风险和约束

- OCR 结果会误识别，必须继续依赖 ESI 查询缓存和分类规则；未确认时保持 `unknown`。
- 预警频道文本格式没有统一标准，解析器必须保留 raw text 和诊断信息。
- ESI 需要缓存、限速、退避和失败降级；未查询过的角色才触发 ESI 查询。
- SSO scopes 要最小化，token 文件不能提交。
- 击毁活跃不进入第一版当前威胁判断。
- PostgreSQL 是下一版服务端目标存储，SQLite 作为本地/兼容存储保留。
