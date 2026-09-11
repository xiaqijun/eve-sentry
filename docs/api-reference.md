# API 参考

[人员缓存方案](personnel-cache-plan.md)已支持可选档案读取和异步刷新，认证方式不变。
`alert.updated.hostile_personnel=[]` 是有效的当前名单更正，不等于视觉清空；消费者必须接受
空数组，并优先按角色 ID 去重。公共档案不保存授权主体的联系人声望。

私有 ESI Gateway 的公共查询 envelope 在普通内存缓存和可选 ID 缓存两种模式下均返回
逐实体 `freshness`：键为字符串 ID（`universe/ids` 为归一化姓名），值包含 UTC 秒数
`fetched_at`、`last_validated_at`、`expires_at` 及布尔值 `stale`。时间与返回资料一同保存，
缓存命中及过期回退不重置时间；缺失实体不生成时间信息。未知时间不能作为可信归属，
需通过正常刷新取得有效时间，不能将读取时间填作获取时间。原 `data`、`cache` 字段保持兼容。
归属查询的 `expires_at - fetched_at` 为官方当前的 3600 秒（两种缓存模式一致），读取旧记录不改变
其年龄；这不是名单推送等待时间。不可信归属先刷新再分类，视觉来敌/清空不等待该查询。

管理员 ESI Gateway 观测接口的 `resolver_cache.archive` 为可选对象，包含 `mode`、
`profiles`、`hot_profiles`、`pending_names`、`background_slots`、`degraded`、
`hot_hits/hot_misses`、`database_hits/database_misses`、`refresh_success/refresh_errors`、
`late_results/storage_errors/queue_rejected` 和 `due_by_priority`。积压行包含
`priority/kind/count/oldest_due_at`（UTC 秒）；`upstream_batch_ms/database_item_ms` 为最近一次
批次耗时/每条数据库处理耗时，不是 P95。计数器进程内累计，未出现的计数项可缺省。
功能关闭时不返回 archive，不能解释为档案数为零；没有新增公开人员枚举接口。

### 管理员人员档案配置

`GET /api/v1/admin/personnel-settings` 返回 `{ "settings": {...} }`；
`POST` 同一路径保存配置，返回 `{ "ok": true, "settings": {...} }`。
沿用管理员权限、网页登录 CSRF 和服务密钥只读限制，不允许普通用户访问。POST 请求必须完整包含：

```json
{
  "revision": "从 GET 返回原样携带",
  "values": {
    "mode": "shadow",
    "background_refresh": true,
    "history_backfill": true,
    "background_max": 4
  }
}
```

`mode` 只能为 `off/shadow/on`；两个开关必须是真正的 JSON 布尔值；并发上限为整数 1～4。
缺少字段、未知字段、错误类型返回 400；配置版本过期返回 409 `settings_conflict`，重新 GET 后
确认再保存；缺少 PostgreSQL/ESI 时不能保存非 off 模式，返回 409 `personnel_settings_unavailable`。
配置管理未初始化时返回 503 `personnel_settings_unavailable`；存储失败返回 503
`personnel_settings_storage_error`，不返回数据库凭据或异常细节，需重新读取确认保存结果。
资源初始化失败返回 503 `personnel_switch_failed`，不保存配置、不切换原模式；旧任务尚未回收且
已达到退休资源上限时，重新启用返回 409 `personnel_switch_busy`，稍后重试，关闭操作仍可执行。

响应 `settings` 包含：`values`（已保存或默认配置）、`effective`（当前实例模式与调度配置）、
`revision`（不透明并发版本）、`source`（environment/database）、`restart_required`、`apply_required`、
`available`、`unavailable_reason`、`writable`。当前档案关闭时 effective 的后台开关为 false、
并发为 0，表示未启动调度，不是丢失已保存配置。模式保存后安全在线切换，无需重启；调度设置
下个周期采用，在途任务安全收尾。`restart_required` 为兼容旧调用方保留，恒为 false；
`apply_required` 表示已保存与当前实例运行配置不一致，可 GET 后使用同一 values/revision 再 POST
重试应用。GET 只读，不隐式应用配置。不是跨实例配置广播接口，也不重启服务或更改 SSE 连接。
首次写入及后续更新使用事务内比较更新，并与 `personnel.settings_changed` 审计一起提交。
初始化在状态锁外完成，准备成功且配置/审计事务提交后才发布解析器及运行时；初始化或事务失败
清理未发布资源、保留原运行模式。若提交结果不确定，重新 GET 核对，不假定保存失败。
存储配置优先于启动环境默认值。无变化保存可重试应用但不新增审计；同一旧 revision 不能覆盖后续修改。
观测 `resolver_cache.archive.scheduling` 补充当前配置的两个布尔开关和并发上限；
`background_slots` 仍表示负载调度后的容量，不表示配置上限或即时在途请求数。

默认地址为 `http://127.0.0.1:8765`。桌面客户端和工作台主要使用 `/api/v1`；旧
`/api/*` 兼容路由仍存在；来袭分析读取专用的 `/api/v1/alert-history`，新接入应优先使用
v1。

第三方实时接入的最小示例和断线处理见[预警消息 API 接入指南](alert-api.md)。客户端
按需 OCR 查询的命令下发与 `query_id` 约定见[客户端对接文档](../client/docs/on-demand-ocr-query.md)。

## 认证

桌面客户端和服务账号使用：

```http
Authorization: Bearer eve_xxx
```

网页登录使用会话 Cookie；所有 POST、PUT、DELETE 请求还需携带登录响应或
`GET /api/v1/auth/me` 返回的 `X-CSRF-Token`。

只读服务密钥可访问 `/api/v1/bootstrap`、`/api/v1/events`、
`/api/v1/alert-history`、`/api/v1/hostile-waves`、
`/api/v1/integrations/hostile-systems`，以及按需 OCR 查询的创建和结果接口。
`POST /api/v1/ocr/query` 是唯一允许的有界命令写入例外；它不直接修改持久化情报。

## 公共接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/livez` | 进程存活探针，不访问外部依赖 |
| `GET` | `/api/readyz` | 存储就绪探针，未就绪时返回 `503` |
| `GET` | `/api/health` | 已脱敏的服务、存储、地图、ESI 和事件流状态 |
| `POST` | `/api/v1/auth/login` | 管理员密码登录 |
| `GET` | `/api/v1/auth/esi/start` | 开始普通用户 EVE SSO 登录 |
| `GET` | `/api/v1/auth/esi/callback` | 统一 EVE SSO 回调 |

所有 HTTP 响应都包含 `X-Request-ID`，可用于关联服务端访问日志。可信的本机反向代理
可以传入最长 64 个字符、仅含字母、数字、点、下划线和连字符的请求 ID；其他来源由
服务端重新生成。`/api/health` 的 `events.sse.active_connections` 给出当前 SSE 连接数。
公开健康响应中的 `clients` 只包含数量、类型、状态和最近上报时间等聚合值；客户端 ID、
主机名、角色名、星系和心跳详情只在受保护的客户端管理接口中返回。

## 账号与管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/auth/me` | 当前用户和 CSRF token |
| `POST` | `/api/v1/auth/logout` | 退出登录 |
| `POST` | `/api/v1/auth/password` | 修改管理员密码 |
| `GET/POST` | `/api/v1/me/keys` | 列出或创建桌面密钥 |
| `DELETE` | `/api/v1/me/keys/{id}` | 吊销密钥 |
| `POST` | `/api/v1/me/keys/{id}/enable` | 重新启用可恢复密钥 |
| `DELETE` | `/api/v1/me/keys/{id}/record` | 删除密钥记录 |
| `POST` | `/api/v1/client/identity-checks` | 幂等提交本地日志文件名中的角色 ID 并立即返回任务状态；身份校验在服务端异步执行 |
| `POST` | `/api/v1/client/identity-check` | 旧版同步身份校验，仅用于滚动升级兼容 |
| `GET/POST` | `/api/v1/admin/users` | 用户列表和创建用户 |
| `GET` | `/api/v1/admin/clients` | 管理员读取包含归属信息的完整客户端心跳和状态 |
| `GET` | `/api/v1/admin/esi-gateway` | ESI Gateway 健康和客户端指标（管理员） |
| `GET/POST` | `/api/v1/admin/security-settings` | 查看或切换服务端密钥风控 |
| `POST` | `/api/v1/admin/users/{id}/status` | 启用或禁用用户 |
| `POST` | `/api/v1/admin/users/{id}/reset-password` | 重置管理员密码 |
| `DELETE` | `/api/v1/admin/users/{id}` | 删除用户 |
| `POST` | `/api/v1/admin/users/{id}/keys` | 管理员为用户创建设备或只读服务密钥；目标用户无需先登录 ESI |
| `POST` | `/api/v1/admin/users/{id}/service-keys` | 创建只读服务密钥 |
| `POST` | `/api/v1/admin/users/{id}/characters` | 添加用户角色白名单 |
| `DELETE` | `/api/v1/admin/users/{id}/characters/{character_id}` | 删除角色白名单 |
| `GET/POST` | `/api/v1/admin/corporations` | 列出或添加允许军团 |
| `DELETE` | `/api/v1/admin/corporations/{corporation_id}` | 删除允许军团 |
| `GET` | `/api/v1/admin/audit` | 审计日志 |

新客户端提交：

```json
{
  "character_ids": [2112345678, 2112345679],
  "client_id": "detector-client:example"
}
```

角色 ID 来自 `Local_..._<character_id>.txt` 或 `本地_..._<character_id>.txt` 文件名末尾。
异步身份接口在任务排队、处理中或等待重试时返回 `202`，已验证时返回 `200`，并在
`characters` 中携带服务端通过 ESI 按 ID 取得的角色名和军团资料。客户端可用相同 ID 集合
重复提交来读取状态，不需要保存任务 ID；服务端按设备密钥和规范化 ID 集合保证幂等。
旧客户端提交的 `characters` 角色名数组仍受支持，但新客户端不再依赖名称搜索。
关闭服务端密钥风控时，这两个身份接口直接返回已确认状态和 `skipped=true`，不会访问 ESI
或创建后台任务。

## 监控与事件

第三方程序接收预警时，优先阅读[预警消息 API 接入指南](alert-api.md)。该指南包含只读
服务密钥权限、SSE 事件类型、断线续传、轮询接口和可运行示例。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/hostile-presence` | 立即上传当前红色敌对图标数量，`0` 表示清空 |
| `POST` | `/api/v1/ocr/snapshot` | 上传完整 OCR 文本名单；可携带按需查询 `query_id`，不携带或改变 Presence 敌对人数 |
| `POST` | `/api/v1/ocr/query` | 请求父 detector 心跳在线且已标记监控的目标执行一次独立 OCR（服务密钥可调用） |
| `GET` | `/api/v1/ocr/query/{query_id}` | 查询独立 OCR 请求的聚合结果 |
| `POST` | `/api/v1/clients/heartbeats` | 上传客户端状态、窗口目标和最近异常 |
| `GET` | `/api/v1/clients` | 在线客户端和聚合状态 |
| `GET` | `/api/v1/active-intel` | 当前实时情报 |
| `GET` | `/api/v1/alert-history` | 按时间分页读取报表告警历史；生产 PostgreSQL 使用独立历史路径 |
| `GET` | `/api/v1/hostile-waves` | 按“出现至清空”记录的敌对星系波次 |
| `GET` | `/api/v1/bootstrap` | Web 和机器人使用的完整初始快照；包含 `clients`、规范化的 `monitoring_nodes` 及其版本 |
| `GET` | `/api/v1/events` | SSE 实时事件流 |
| `GET` | `/api/v1/integrations/hostile-systems` | 第三方软件读取当前存在敌对的星系 |
| `GET` | `/api/v1/alerts` | 当前敌对告警 |
| `GET` | `/api/v1/alerts/{id}` | 单条告警详情 |
| `GET/POST` | `/api/v1/reports` | 历史上报读取和写入 |
| `GET/POST` | `/api/v1/observations` | 规范化观察记录读取和写入 |
| `POST` | `/api/v1/channel-lines` | 独立频道客户端上传日志行 |

### 客户端心跳与采集状态

检测客户端心跳的 `details.targets` 会在窗口采集状态已确定时包含
`capture_online` 和 `capture_failure_count`；`capture_online=false` 表示该 EVE 窗口已
关闭或后台画面连续不可用，并会把归一化监控节点状态强制设为 `offline`。该目标仍属于父
客户端心跳和 `monitoring_nodes`，直到父心跳年龄进入 `removed`；采集恢复后客户端会立即
发送新的心跳。无法归一化星系名称的目标不会出现在监控节点集合中。

### Presence

`POST /api/v1/hostile-presence` 的请求体包含 `client_id`、`source_instance`、
`system_name`、`hostile_icon_count`，并可带 `system_id`、`seen_at`、`snapshot_id`、
`sequence`、`captured_at`、`presence_version` 和 `presence_state_id`。该接口只维护当前
星系数量状态，不创建虚假人员报告。`presence_version` 与 `presence_state_id` 用于幂等
对账：旧版本不会覆盖新状态，重复版本可成功确认但不重写状态。实时敌对人数只能由直接
Presence 上报或 heartbeat 中携带的同版本 Presence 对账状态改变；OCR 不能改变人数。

### OCR 快照

`POST /api/v1/ocr/snapshot` 的请求体包含 `client_id`、`source_instance`、`system_name` 和
完整 OCR 文本数组 `names`，可带 `system_id`、`seen_at`、`confidence`、`snapshot_id`、
`sequence`、`captured_at` 和 `query_id`。OCR 请求不得发送 `hostile_icon_count`，也不得用于
创建、续期、清除或覆盖 Presence。客户端方法仍接受的 `hostile_icon_count`、
`ocr_candidates` 和 `hostile_icons` 是废弃的兼容参数，序列化前会被移除。服务端对每个
文本独立请求 ESI，按 standing、军团/联盟和白名单生成敌对、友军或未解析状态。
`query_id` 标记一次性按需 OCR 查询；空 `names` 仍是有效查询结果。

`POST /api/v1/ocr/query` 用于一次性查询当前监控目标的本地名单。服务端选择父 detector
heartbeat 仍在线、且 `monitoring=true` 的目标；当前不会再按单目标 `capture_online` 过滤，
所以 `capture_online=false` 的目标仍可能计入 `expected_clients`，但无法返回结果。请求体支持
可选 `system_name`；传入时按星系名称大小写不敏感精确匹配目标，未找到时返回 `409`；未传时
查询全部符合条件的目标。服务端返回 `query_id` 后，会在后续客户端心跳响应中下发
`ocr_query` 命令；客户端执行一次全帧 OCR，并在上传中携带相同的 `query_id`。调用方通过
`GET /api/v1/ocr/query/{query_id}` 轮询，
直到 `status` 为 `completed` 或 `timed_out`。请求体可携带 `name`、`corporation` 或
`alliance` 过滤条件；过滤仍以服务端识别结果为准。服务端会在目标上传结果前的后续心跳中
重试下发同一命令，客户端必须按 `query_id` 幂等去重；目标客户端 ID 与父 detector
heartbeat ID 均可用于领取命令。

```json
{
  "system_name": "S-KSWL",
  "name": "Alice",
  "timeout_seconds": 30
}
```

`name`、`corporation` 和 `alliance` 只是服务端结果筛选条件；客户端始终上传本次完整原始
名单。`system_name` 是命令目标选择条件，并会出现在创建响应、心跳命令和聚合状态中。

`GET /api/v1/admin/esi-gateway` 返回 Gateway 的运行摘要、业务缓存和进程内远端调用指标。
`resolver_cache.personnel` 按收到的新 OCR 人员逐个统计 `lookups`、`hits`、`misses`、
`hit_rate`，是人员名单缓存命中率的主口径，并区分新鲜、过期和负缓存命中；`namespaces`
提供底层名称、角色、军团、联盟和星系资料缓存以及有效/过期条目。一次包含多个未缓存名称的
Gateway 批量请求仍只算一次远端请求。
Gateway 的 `requests` 是总请求数，`upstream_requests` 是实际访问 ESI 的次数，
`cache_hits`、`cache_misses` 和 `cache_hit_rate` 使用同一总请求口径；`request_rate_per_second`
为最近 60 秒请求率，`endpoints` 提供端点级拆分。`client_metrics` 同样包含 `totals`、
`endpoints` 和兼容保留的 `counts`/`durations_ms`。

历史报告和观察记录支持显式游标分页。首个请求传 `cursor=start`，后续请求原样传回
`next_cursor`；`next_cursor` 为 `null` 表示结束。普通列表和分页默认每次最多 100 条，
`limit` 最大 1000。
使用后续游标时应保持 `source`、`system` 和 `name` 筛选条件不变。例如：

```http
GET /api/v1/observations?cursor=start&limit=100&source=intel_channel
GET /api/v1/observations?cursor=eyJ...&limit=100&source=intel_channel
```

未传 `cursor` 时保留原响应结构，但结果同样有默认上限。PostgreSQL 在收到 `limit` 时
直接使用数据库键集查询，不会先读取全部历史再在 Python 中截断。需要遍历完整历史时
必须使用 `cursor=start` 和后续 `next_cursor`，不能依赖无上限列表响应。

兼容接口 `/api/alerts`、v1 告警列表和 `/api/v1/alert-history` 默认最多返回 100 条，显式
`limit` 最大 1000。
告警生成按接收时间倒序处理，满足 `since`、筛选条件和数量后立即停止，不会为一次历史
查询重新评分全部热报告。
`/api/v1/active-intel` 同样默认最多返回 100 条；需要更多活跃项时必须显式传递 `limit`。
`/api/v1/alert-history` 支持 `since`、`limit`、`min_score` 和 `min_level`。PostgreSQL
部署会直接按 `received_at` 索引分页，并使用报告快照和本地缓存重建分类，避免历史查询
触发外部 ESI/zKill 请求或污染实时告警缓存。返回结果会排除 `classification=white` 以及
带有 `friendly_*` 证据的记录；旧 detector `red` 快照会用持久化 ESI 身份资料重新核验，
避免已确认友军出现在敌对历史中。
`/api/v1/hostile-waves` 支持 `since` 和 `limit`。`since` 会返回结束时间晚于该时刻或仍在
进行中的波次；每个星系从敌对总数由 0 变为大于 0 时创建波次，回到 0 时写入
`cleared_at`，之后再次出现会创建新的 `id`。该接口使用独立 PostgreSQL 生命周期表，
不会恢复启动时的全量 active-intel 历史加载。每条波次的 `peak_hostile_count` 是该波次
期间客户端上报的红色图标数量峰值；同一客户端的视觉状态与 OCR 人员行会去重，同星系
多客户端取当前最大值。人员识别失败不会移除已有视觉证据波次。

示例响应：

```json
{
  "schema_version": "hostile_waves.v1",
  "waves": [
    {
      "id": "f27d15bd84d44cd5a7a73435f7de5706",
      "system_name": "S-KSWL",
      "system_id": 30004759,
      "started_at": "2026-08-11T09:30:00+00:00",
      "last_seen_at": "2026-08-11T09:34:12+00:00",
      "cleared_at": "2026-08-11T09:35:03+00:00",
      "active": false,
      "peak_hostile_count": 3,
      "personnel": [
        {
          "character_id": 2118213032,
          "name": "CEKC HA MOPE",
          "identity_status": "resolved",
          "first_seen_at": "2026-08-11T09:31:04+00:00"
        }
      ]
    }
  ],
  "count": 1,
  "generated_at": "2026-08-11T09:36:00+00:00"
}
```

`peak_hostile_count` 是视觉峰值，不等于 OCR 已识别人员数。`personnel` 是服务端在波次生命
周期内累计的、已由 ESI 确认且当前分类为 `red` 的 detector 角色；它不会因后续 OCR 行消失
或告警历史快照不完整而丢失。客户端仍可将 `/api/v1/alert-history` 中的
`verified_characters` 作为补充信息来源。

敌对告警的 `verified_characters` 始终保留 `character_id` 和 `name`。实时人员解析已停止
请求 zKillboard 战绩；机器人和星图通过角色 ID 直接生成
`https://zkillboard.com/character/{character_id}/`，不需要查询统计接口。
星图不再显示威胁等级、分数和评分条。协议中的 `score`、`level` 暂时保留为敌我分类的兼容值，
并非人员战斗能力评分，不触发额外请求；`classification` 及名单/声望分类逻辑不变。

旧历史记录仍可能包含可选的 `zkill` 对象（兼容示例，不代表当前仍会抓取）：

```json
{
  "character_id": 443630591,
  "name": "Example Pilot",
  "zkill": {
    "source": "zkillboard",
    "danger_ratio": 68,
    "gang_ratio": 99,
    "solo_ratio": 0,
    "ships_destroyed": 1043,
    "ships_lost": 179,
    "isk_destroyed": 1888837094625,
    "isk_lost": 14433360680,
    "fetched_at": "2026-08-03T00:00:00Z"
  }
}
```

`zkill` 缺失是当前正常行为，不应触发补查或重试。消费者必须把字段视为可选，
不能使用告警 `score` 推断或回填 `danger_ratio`。已有历史统计不删除，
不影响 `classification`、告警生成或确认状态。机器人独立的手动战报分析不属于实时人员解析。

SSE 常用查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `since` | 空 | ISO 8601 时间回退游标；主要用于没有可解析持久化事件 ID 时恢复 |
| `limit` | `50` | 普通告警与持久状态事件的单页读取上限，最大 `1000`；为追平一致快照水位，一次响应可能连续读取多页状态事件 |
| `timeout` | `30` | 本次 SSE 响应最长秒数，范围 `0–300`；`0` 读取当前一轮后结束 |
| `heartbeat` | `15` | SSE 注释心跳秒数，范围 `0–60`；`0` 表示关闭 |
| `bootstrap` | `false` | 是否请求初始权威快照 |

客户端必须保存并推进事件游标，不能在每次重连时反复拉取历史事件。服务端优先读取
`Last-Event-ID`：

- `state:<sequence>` 直接恢复持久化状态事件序列；
- 已持久化的 alert/report ID 会解析到对应流游标；
- ISO 8601 事件 ID 可作为时间游标；
- `presence_*` 是合成的非持久化 ID，不能解析成报告游标，此时由权威 `bootstrap` 对账，
  并可使用 `since` 回退。

客户端取得 `state:<sequence>` 后只允许更高序号替换它，不能让后续报告、Presence 或节点
事件 ID 降级覆盖。无 `Last-Event-ID`、无 `since` 且请求 Bootstrap 表示初始化到当前状态；
只有显式 `state:0` 才要求从保留日志起点完整重放；任何显式 `state:*` 均禁用 `since`
时间过滤。

SSE wire 事件与内部状态事件的映射如下：

| `data.event_type` | wire `event` |
| --- | --- |
| `alert.entered` | `alert` |
| `alert.updated` | `alert` |
| `alert.cleared` | `safe` |

`monitoring_node` 是节点变化通知，不是名为 `node.updated` 的 wire 事件；消费者应使用随后
到达的 `bootstrap` 作为节点状态权威快照。

PostgreSQL 从同一数据库视图取得活动状态和已提交事件水位 `W`。恢复游标落后时，服务端
按序发送全部 `seq <= W` 的持久事件，再发送 ID 为 `state:W` 的 Bootstrap；快照尚未就绪、
水位落后于已读取事件或客户端游标时不发送旧 Bootstrap。消费者应在事件处理成功后才确认
`state:<sequence>`，并把 `safe` 作为幂等清空。该视图中的告警只由同批报告及其持久化身份
元数据重建，不会补读实时人物缓存；OCR/ESI 丰富与对应 active/事件也以单事务发布。
派生报告/Presence 事件之后，服务端可发送仅含 `id: state:W` 的无数据控制块，以恢复原生
EventSource 的可靠重连 ID；该块不会派发业务事件，消费者应忽略无 `data` 块。

预警客户端不调用完整 `/api/v1/bootstrap`；它在 SSE 上请求精简快照，只包含活跃情报、
活跃告警和监控节点。生成该快照时只处理活跃情报引用的报告。

服务端共享缓存原子发布状态、水位及活动报告游标索引；新鲜缓存读取不等待构建锁。
无游标及旧游标连接使用同一索引，标准 PostgreSQL 报告恢复只查持久化字段，时间戳 ID
直接作为时间游标。缓存仍按状态变更失效并保留 1 秒 TTL；刷新未就绪或水位不足时不发送
Bootstrap。这些内部优化不改变事件字段、分页、过滤和重放顺序。

### 第三方敌对星系接口

第三方软件可使用只读服务密钥轮询当前存在敌对的星系：

```http
GET /api/v1/integrations/hostile-systems
Authorization: Bearer eve_xxx
```

```json
{
  "schema_version": "hostile_systems.v1",
  "generated_at": "2026-08-03T12:00:00+00:00",
  "count": 2,
  "systems": ["S-KSWL", "Tama"]
}
```

`systems` 只包含当前仍有敌对证据的星系名称，按名称排序；星系清空后会从下一次响应中
消失。接口不返回角色、客户端、评分或其他内部告警信息。需要实时预警消息、清空事件和
断线续传时，使用[预警消息 API 接入指南](alert-api.md)中的 SSE 接口。

## 地图、配置与 ESI

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/map` | 当前地图快照 |
| `GET` | `/api/v1/map/neighborhood` | 按中心星系和跳数返回局部地图拓扑 |
| `GET` | `/api/v1/map/systems/{system_id}` | 星系节点详情 |
| `GET/PUT` | `/api/v1/config` | 敌我分类配置 |
| `GET` | `/api/v1/characters/{character_id}` | 角色资料 |
| `GET` | `/api/v1/characters/by-name/{name}` | 按准确名称解析角色 |
| `GET` | `/api/v1/systems/{system_id}` | 星系资料 |
| `GET` | `/api/v1/systems/by-name/{name}` | 按名称解析星系 |
| `GET` | `/api/v1/esi/status` | ESI 配置和授权状态 |
| `GET` | `/api/v1/esi/session` | 位置、contacts 和授权账号声望快照 |
| `GET/POST` | `/api/v1/esi/login` | 态势页 ESI 授权状态和启动 |

`/api/v1/esi/status` 和 `/api/health` 的 ESI 摘要会同时返回 `expired` 与
`refreshable`。`expired=true, refreshable=true` 只表示短时 access token 已过期，后续
认证 ESI 请求会使用保存的 refresh token 自动刷新，不代表账号授权失效；只有
`refreshable=false` 或返回 `error` 时才需要重新授权或检查令牌存储。

`/api/v1/esi/session` 的 `standings` 来自授权角色的 ESI standings 接口，服务端按
`character_id` 保存一份完整快照，默认缓存 600 秒，可通过
`EVE_SENTRY_SERVER_ESI_STANDINGS_TTL` 配置为 300–900 秒。缓存命中时不重复请求；过期刷新
失败时返回最近一次旧快照。该请求始终由服务端携带授权 token 发起，公共 ESI Gateway 不接收
授权 token，也不按联系人数量拆分请求。

`/api/v1/kill-activity/*` 仅保留兼容行为；实时人员解析不再抓取 zKillboard 统计，
`verified_characters[].zkill` 仅可能来自旧历史数据，不提供同步补查接口。
`/api/v1/map/neighborhood` 接受逗号分隔的 `systems`、`system_ids` 和 `hops` 参数，
`hops` 默认 `3` 且最大为 `5`。响应只包含任一中心星系指定跳数内的节点和节点间连线；
预警浮窗使用该接口，避免传输完整地图。Windows 预警客户端在后台 `AlertMapWorker` 中
使用 5 秒请求超时；同一时刻只运行一个拓扑请求，请求期间的多次变化只保留最后一个待处理
请求，界面线程不会被网络请求阻塞。

地图源支持 `builtin`、`manual` 和 `sde`。生产推荐使用官方 SDE，并通过
`scripts/sync_sde.py` 同步到服务端运行目录。
