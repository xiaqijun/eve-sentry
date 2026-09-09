# 可靠推送与按需 OCR 实施计划

本文是服务端、监控客户端、预警客户端和 QQ 机器人共用的推送方案。后续涉及
Presence、OCR、Heartbeat、SSE、告警、清空、节点或按需 OCR 的修改，都必须先对照本文。
文中标为“现行”或列为已勾选的内容表示截至 v1.0.69 已实现；标为“目标”或未勾选的内容
仍是后续设计，不能当作生产现状。

## 1. 结论

整体流程没有概念性冲突，关键边界已经确认：

- 红色敌对图标检测和 OCR 使用同一张截图的两条分支，不是先检测后重新截图；
- Presence 是实时预警和地图敌对人数的唯一权威依据，必须先于 OCR、ESI、zKill 和人员名单补全；
- OCR 只上传人员名单，不携带 `hostile_icon_count`，也不得创建、刷新或清空 Presence；
- 普通 OCR 是客户端自动补充名单；
- 按需 OCR 是 QQ 查询触发的一次性 OCR，不依赖红色图标，也不受常规 OCR 开关影响；
- 客户端连续两帧检测为零后，只发送一条权威的 Presence 清空；
- HTTP Heartbeat 默认为 10 秒、允许配置但最短为 5 秒，并携带全部监控节点的最新
  Presence 状态用于丢包对账；
- Bootstrap 只负责初始化、重连恢复和状态指纹变化对账，不承担实时事件生成；
- 清空事件必须由服务端状态变化生成，不能由每个 SSE 连接临时推导；
- 星图客户端和 QQ 机器人都消费服务端权威事件，但 QQ 的投递失败不能影响星图；
- 节点状态采用第一次缺失黄色、第二次缺失灰色、第三次缺失移除；
- 节点离线导致的情报移除不播报为敌对清空，也不发送人员清空消息；
- WebSocket 不是当前必需，继续使用 HTTP 上传 + SSE 下行。

截至 v1.0.65，现行实现已经包含 PostgreSQL `intel_events` 事件表、进入/更新/清空事件的
事务内追加、SSE 首字节立即返回及事件游标重放、星图客户端持久化 `Last-Event-ID`、事件型
Heartbeat 限流、有界后台地图加载和节点健康状态刷新；默认视觉扫描间隔为 2 秒。
`cursor_reset`、Redis Stream Dispatcher 和底层上传连接的完全拆分仍属于后续目标，不能按
现行能力描述。

## 2. 完整流程

```text
监控客户端每轮截图一次
  ├─ 红色敌对图标检测
  │    ├─ hostile_count 变化 → 立即更新本地状态
  │    └─ 立即进入 Presence Control Lane
  │
  └─ 名单画面指纹和 OCR 判断
       ├─ 不需要 OCR → 结束本轮
       └─ 需要 OCR → 复用当前截图执行 OCR

Presence Control Lane
  → POST /api/v1/hostile-presence
  → 服务端更新 active_intel / hostile_waves
  → 服务端写入事件日志
  → SSE 发送 wire alert / safe（data.event_type 保留 alert.entered / updated / cleared）
  → 星图客户端立即更新
  → 现行 QQ 机器人在 SSE 消费循环中处理并投递消息
  → Redis 保存时间游标、事件 ID、状态和去重数据

目标 QQ 投递链路（尚未落地）
  → Redis high/normal stream
  → QQ Dispatcher 独立投递消息

OCR Normal Lane
  → POST /api/v1/ocr/snapshot
  → 服务端异步解析角色、军团、联盟、standings
  → 生成 alert.updated
  → 星图和 QQ 更新人员名单

Heartbeat Reconciliation Lane（默认每 10 秒）
  → POST /api/v1/clients/heartbeats
  → 携带全部 monitoring_targets 的 Presence 状态和版本
  → 服务端只应用比当前更新的状态
  → 修复实时 Presence 请求丢失造成的不一致

QQ 按需查询
  → 服务端创建 query_id
  → detector heartbeat 响应下发命令
  → 指定客户端复用当前截图执行一次 OCR
  → 上传带 query_id 的 OCR 快照
  → 服务端聚合查询结果
  → QQ 返回查询结果
```

## 3. 三类 OCR 的边界

### 3.1 红色图标检测

红色图标检测使用当前截图，目标是最快得到视觉敌对数量。它不负责判断具体人员，也不
等待 OCR 或 ESI。

```text
截图 → find_hostile_icons(img) → hostile_count → Presence
```

检测到正数或非零数量变化时立即上传。检测到零值时先在客户端本地确认，只有连续两帧
均为零才发送一条 `hostile_icon_count=0`。两帧确认不是两次网络请求，服务端收到零值后
必须立即清空，不再等待第二次请求。

### 3.2 普通 OCR

普通 OCR 由监控线程根据状态变化自动触发：

- 敌对数量变化；
- 名单区域画面变化；
- OCR 短时重试；
- 常规监控需要补充人员名单。

普通 OCR 只提供增效信息，失败不能撤销 Presence，也不能把系统直接判定为清空。
OCR 请求不得携带 `hostile_icon_count`；敌对人数只能来自 Presence。OCR 结果只有在相同
`client_id + system_name` 存在活动 Presence 时，才能绑定为当前敌对人员。Presence 已经
清空或节点已经离线时，延迟到达的 OCR 只能作为查询或历史结果，不能重新激活地图和波次。

### 3.3 按需 OCR

按需 OCR 是外部查询触发的一次性动作：

```text
QQ 查询指令
  → 服务端创建 query_id
  → heartbeat 下发到目标 client_id
  → 客户端执行一次 OCR
  → 上传 query_id + names
  → 服务端识别并聚合
  → QQ 返回结果
```

按需 OCR 的约束：

- 即使没有红色敌对图标，也必须执行；
- 即使常规 OCR 关闭，也必须执行；
- 不得覆盖普通 OCR 快照队列；
- 同一 `query_id` 重复下发时客户端只执行一次；
- 查询结果不能替代实时 Presence；
- 客户端上传必须使用命令中的目标 `client_id`。

QQ 查询的完整菜单、指定星系目标选择、所有节点名单、预警节点快速查询和上线监测规则，
统一以 [QQ 查询与上线监测实施方案](qq-query-and-online-monitoring-plan.md) 为准。

## 4. 服务端事件模型

服务端是敌我分类、人员名单和波次状态的唯一权威来源。

### 4.1 事件类型

| 内部状态事件 | 触发条件 | 优先级 | 说明 |
| --- | --- | --- | --- |
| `alert.entered` | 系统从 0 变为大于 0 | P0 | 立即发送，不等待 OCR |
| `alert.cleared` | 系统从大于 0 变为 0 | P0 | 必须持久化，不能由 SSE 推导 |
| `alert.updated` | 数量、身份或确认名单变化 | P1 | 允许短窗口合并 |
| `node.updated` | 节点上线、下线、换星系 | P1 | 目标持久事件，现行服务端尚未生成 |

现行状态与节点变化 SSE wire 事件名为 `alert`、`safe` 和 `monitoring_node`。前三种持久状态事件仍在
`data.event_type` 中保留内部名称；`alert.entered`、`alert.updated` 映射到 wire `alert`，
`alert.cleared` 映射到 wire `safe`。节点变化当前由连接内生成的 `monitoring_node` 通知，
随后以权威 `bootstrap` 快照对账。

`alert.cleared` 必须包含 `clear_reason`。只有 `clear_reason=visual_confirmed`，即客户端连续
两帧确认零值时，机器人才能发送正常的“星系清空”消息。节点离线引起的状态移除使用
`clear_reason=node_offline`：星图删除陈旧红色，机器人不发送敌对清空或人员清空消息，
节点表格本身就是用户侧通知。

### 4.2 事务边界

状态和事件必须在同一数据库事务中提交：

```text
BEGIN
  active_intel
  hostile_waves
  intel_reports
  intel_events
COMMIT
  → 唤醒 SSE
```

禁止在全局内存锁中执行 PostgreSQL 网络 I/O，也禁止 SSE 连接自行生成 `safe` 或扫描全部
历史报告。

### 4.3 事件载荷

每个事件至少包含：

```json
{
  "seq": 12345,
  "event_key": "alert:S-KSWL:entered:wave_abc",
  "event_type": "alert.entered",
  "entity_key": "S-KSWL",
  "occurred_at": "2026-09-06T01:00:00+00:00",
  "payload": {
    "system_name": "S-KSWL",
    "system_id": 30000123,
    "wave_id": "wave_abc",
    "hostile_count": 2,
    "active": true,
    "hostile_personnel": []
  }
}
```

`hostile_count` 是视觉/状态聚合人数；`hostile_personnel` 只能使用服务端完成身份核验和
敌我判断后的名单。单条 OCR 报告的 `names` 不能作为完整敌对名单。

## 5. 实时性目标

目标从画面出现开始计算：

| 阶段 | 目标 |
| --- | ---: |
| 画面出现 → 客户端检测 | p95 ≤ 2 秒 |
| 检测 → Presence 上传 | p95 ≤ 300ms |
| 服务端收到 → 事件提交 | p95 ≤ 300ms |
| 事件提交 → SSE 发出 | p95 ≤ 300ms |
| SSE 到达 → 星图显示 | p95 ≤ 100ms |
| 画面出现 → 星图服务端预警 | p95 ≤ 3 秒 |
| 服务端收到 → QQ 首条来敌 | p95 ≤ 2 秒 |
| 敌对消失 → 客户端两帧确认 | p95 ≤ 4 秒 |
| 两帧确认 → 星图清空 | p95 ≤ 1 秒 |

实时性规则：

- P0 进入、清空和数量变化不经过合并窗口；
- P1 人员和身份更新最多延迟 200～300ms；
- 节点更新最多延迟 300～500ms；
- Bootstrap 在连接、重连以及服务端状态指纹变化时对账，不作为正常实时事件生成路径。

节点心跳默认 10 秒、允许配置但最短 5 秒。服务端在每个缺失边界增加半个心跳周期、最多
5 秒的调度宽限；按默认值计算，第一次约 15 秒进入连接异常，第二次约 25 秒进入离线并
移除该节点贡献的情报，第三次约 35 秒从节点列表删除。这段宽限只吸收上传排队、网络和
定时器抖动，不计作额外心跳周期，避免健康节点因心跳晚到 1～3 秒而反复推送
“连接异常 → 正常”。

## 6. 客户端通道

客户端必须分离以下通道：

```text
Presence Control Lane
  ├─ 敌对进入
  ├─ 数量变化
  ├─ 清空 0
  └─ 星系变化

OCR Normal Lane
  ├─ 普通名单 OCR
  ├─ 按需 OCR
  └─ 身份补全

Heartbeat Lane
  ├─ 节点在线状态
  ├─ 客户端运行状态
  ├─ 全部监控节点的 Presence 状态对账
  └─ 查询命令

SSE Reader
  └─ 接收服务端事件
```

Presence 不能等待 OCR、Heartbeat 或已经开始的普通上传请求。清空 `0` 与非零进入拥有
相同优先级。客户端界面必须分别显示 SSE 状态和上传状态，OCR 失败不能显示为 SSE 连接异常。

每个 Presence 状态必须包含 `presence_version`、`presence_state_id` 和 `captured_at`。
实时 `/hostile-presence` 与后续心跳携带同一组状态标识：服务端只接受更高版本，相同版本
作为幂等重复忽略，更低版本不得覆盖新状态。实时请求必须持久化重试直到收到服务端 ACK；
如果实时请求丢失，下一次默认 10 秒心跳使用相同状态完成对账，因此不再单独增加周期
Presence 请求。

现行默认扫描策略为每 2 秒检测红色图标；可配置范围为 1～10 秒。OCR 继续异步执行；同一
节点只保留最新 OCR 快照。

## 7. SSE 与游标

SSE 建立后立即发送 `: connected`，并按可恢复游标读取事件：

```text
建立连接
  → connected
  → 解析 Last-Event-ID
  → 发送游标之后的状态事件及权威 bootstrap（消费者不得依赖固定先后）
  → 空闲 comment keepalive
```

现行空闲 keepalive 和应用 Heartbeat 是两套机制：星图客户端请求 1 秒 SSE comment，机器人
请求 15 秒，服务端缺省也是 15 秒；客户端向 `/api/v1/clients/heartbeats` 发送的 HTTP
Heartbeat 默认为 10 秒。

现行游标规则：

- 持久化状态事件使用 `state:<sequence>`，并按 sequence 顺序发送；
- 已持久化的 alert/report ID 可以解析到对应报告游标；
- ISO 8601 事件 ID 可以作为时间游标；
- `presence_*` 是合成 ID，不能直接解析成持久报告游标，此时由权威 Bootstrap 对账，并可用
  `since` 作为时间回退；
- 星图客户端在事件完整处理后把最高 `state:<sequence>` 写入 `alert_client_state.json`，
  后续非持久 ID 和更低序号不得覆盖，重连时通过 `Last-Event-ID` 恢复；机器人成功处理事件后
  把 `state:<sequence>` 写入 Redis，重连时同时
  发送该 `Last-Event-ID` 与单调递增的时间游标 `since`，其他兼容事件仍由时间游标回退。
  持久状态序号不设置投递去重 TTL，避免长时间安静后失去恢复位置。

其余要求：

- 断线重连从最后完整处理的游标继续；
- 目标能力（尚未实现）：游标过期时发送 `cursor_reset` 和最新 Bootstrap；
- PostgreSQL 模式下无论 `active_only` 是否启用，都必须读取持久化 `intel_events`；
- `active_only` 只能限制历史报告，不能过滤 `alert.cleared` 等状态事件；目标持久
  `node.updated` 落地后也必须遵守此规则；
- `clear_reason=node_offline` 的清理事件仍需发送给星图用于移除陈旧状态，但机器人不得将其
  格式化为敌对清空消息；
- 不得在每个 SSE 连接中调用无界的活动告警扫描；
- 服务端不得根据每个 SSE 连接的前后快照临时合成持久清空事件；消费者漏收 `safe` 时，
  可根据权威 Bootstrap 做一次幂等补偿，后续同一持久清空只确认不重复通知；
- 同一轮发送持久状态事件和 Bootstrap 时，服务端必须从一条数据库一致快照取得状态与水位
  `W`，按页完整重放 `seq <= W` 后再发送 `state:W` Bootstrap；消费者对已由 Bootstrap
  应用的清空只确认一次，时间游标不得倒退。

现行内部状态事件到 SSE wire 事件的映射为：

```text
alert.entered → alert
alert.updated → alert
alert.cleared → safe
```

现行 `monitoring_node` 由连接内节点快照差异生成；`node.updated → monitoring_node` 是目标
持久节点事件的兼容映射，不表示当前服务端已经生成 `node.updated`。

## 8. 星图预警客户端

星图客户端收到 SSE 后只做轻量处理：

```text
读取 → 解析 → 提交 UI 信号 → 更新星图/浮窗/声音
```

P0 事件不能等待 Bootstrap、OCR、ESI 或 zKill。星图在 SSE 连接和重连时请求 Bootstrap；
服务端状态指纹变化时会在同一连接中再次发送权威快照。客户端没有固定 30 秒 Bootstrap
轮询。每条事件完整处理后只推进可靠状态序号，重连时发送 `Last-Event-ID`；全新客户端没有
游标时直接初始化到当前快照水位，不从 `state:0` 回放整个保留期。

局部星图由后台 `AlertMapWorker` 请求 `/api/v1/map/neighborhood`，超时 5 秒、跳数最大 5，
不会阻塞 SSE 或 UI；请求运行期间的多次变化只保留最后一个待处理请求。

监控节点采用以下星图状态：

| 连续缺失心跳 | 服务端状态 | 星图显示 | 是否计入在线 | 情报处理 |
| ---: | --- | --- | :---: | --- |
| 0 次 | `online` | 绿色 | 是 | 正常 |
| 1 次 | `degraded` | 黄色 | 否 | 暂时保留最后情报 10 秒 |
| 2 次 | `offline` | 灰色 | 否 | 移除该节点贡献的 Presence 和 OCR 情报 |
| 3 次 | `removed` | 删除节点 | 否 | 删除临时节点状态 |

星图不能使用红色表示节点离线，因为红色已经表示敌对。第一次缺失必须立刻从“正常在线”
状态降级为黄色，避免用户把它当成仍在正常工作。第二次缺失后地图删除该节点贡献的陈旧
敌对状态；如果相同星系仍有其他正常节点报告敌对，星系继续保持红色。
监控目标主动报告 `capture_online=false` 时会直接进入 `offline`，不要求先经过 `degraded`。

规范化后的 `health_status` 已纳入账号刷新签名；即使账号、星系、敌对人数和监控开关都没
变化，`online`、`degraded`、`offline` 之间切换也必须更新覆盖层并重新加载局部星图。

## 9. QQ 机器人

现行机器人在 SSE 消费循环中直接处理事件并调用 QQ API。Redis 保存时间游标、最后事件
ID、活动状态和投递去重数据；当前没有 high/normal/dead Stream，也没有独立 Dispatcher。
机器人在连接和重连时请求 Bootstrap，并消费服务端状态指纹变化产生的后续 Bootstrap，
没有独立 30 秒轮询。

目标持久投递架构（尚未落地）拆为三个组件：

```text
SSE Reader → Redis Stream Writer → QQ Dispatcher
```

目标要求 SSE Reader 不调用 QQ API，事件写入 Redis Stream 成功后才推进游标。

Redis 队列：

```text
eve:sentry:events:high
eve:sentry:events:normal
eve:sentry:events:dead
```

- High：`alert.entered`、`alert.cleared`，立即发送；
- Normal：`alert.updated`、`node.updated`，允许 200～300ms 合并；
- 幂等键：`event_key + group_openid`；
- QQ 失败独立重试，超过次数进入死信；
- QQ 失败不能阻塞 SSE Reader，也不能影响星图；
- Bootstrap 对账发现漏报时生成幂等补偿任务。

### 9.1 监控节点表格

节点状态消息统一使用与预警时间相同的 QQ Markdown 表格渲染通道，不再发送多行纯文本：

```markdown
在线监控节点｜6

| 节点 | 状态 | 星系 | 敌对人数 |
| :-- | :--: | :-- | --: |
| 监控节点 1 | 🟢 正常 | 30-D5G | 0 |
| 监控节点 2 | 🟢 正常 | HB-FSO | 0 |
| 监控节点 3 | 🟢 正常 | J1-KJP | 0 |
| 监控节点 4 | 🟢 正常 | NCG-PW | 1 |
| 监控节点 5 | 🟢 正常 | R-YWID | 0 |
| 监控节点 6 | 🟢 正常 | S-KSWL | 0 |
```

敌对人数来自 Presence，不能使用 OCR 名单长度。黄色节点显示最后一次人数并标注“上次”，
灰色离线节点显示 `—`，不能用 `0` 暗示已经确认安全。节点按星系名称和稳定 `node_id`
排序，避免状态更新时顺序跳动。

完整表格只在节点上线、第一次缺失、第二次缺失、第三次移除、恢复、换星系或节点配置变化
时发送。普通敌对人数变化继续走预警事件，不重复发送整张节点表格。
`monitoring_nodes_version` 只包含 `node_id + system_name + health_status`，敌对人数不参与版本
计算；相同版本的 Bootstrap 或 SSE 重连不得重复播报节点列表。

节点恢复后的首个心跳使用携带的 Presence 快照立即对账。恢复后人数为零时，只在表格中
显示 `0`，不发送人员清空、“恢复确认安全”或额外节点恢复消息；恢复后人数大于零时，按照
当前聚合状态生成来敌或更新事件。

## 10. 移动事件

服务端不再生成独立的冗余“敌对移动”事件。移动由两个真实状态事件表示：

```text
来源星系 → alert.cleared
目标星系 → alert.entered
目标名单 → alert.updated
```

机器人可以在 200～300ms 内关联来源和目标后合并展示，但底层事件必须保持独立，以便
星图、重放和故障恢复。

## 11. 故障隔离

| 故障 | 必须保持正常的部分 |
| --- | --- |
| OCR 慢或失败 | Presence、Heartbeat、SSE、首条预警 |
| Presence 临时失败 | OCR、SSE；保留最新 Presence 重试 |
| ESI/standings 失败 | 首条进入预警；人员名单可稍后补全 |
| QQ API 慢或失败 | 独立星图消费者不受影响；现行机器人消费可能被拖慢，目标队列落地后独立重试 |
| Redis 重启 | 现行游标与去重依赖 Redis 持久化；目标队列落地后通过 Consumer Group 恢复 Pending |
| SSE 断线 | 星图按持久 `Last-Event-ID` 重连；机器人优先按 Redis 中的 `state:<sequence>` 恢复，并用单调时间游标 `since` 回退 |
| PostgreSQL 暂不可用 | 快速失败，不持有全局锁；客户端保留最新状态 |
| 客户端截图失败 | 上报 capture 状态并清除对应 Presence |
| 单次心跳轻微迟到 | 在半个周期、最多 5 秒的调度宽限内保持在线；真正越过首个边界后才显示连接异常，暂不清除情报 |
| 连续两次心跳缺失 | 节点变灰并移除其情报；不发送敌对或人员清空消息 |
| 连续三次心跳缺失 | 从星图和节点表格删除；恢复后作为节点重新加入 |

## 12. 时间戳和监控

每条 Presence 和事件应关联：

```text
request_id
event_key
client_id
system_name
captured_at
presence_queued_at
presence_sent_at
server_received_at
state_committed_at
sse_written_at
client_received_at
ui_applied_at
qq_delivered_at
```

`qq_queued_at`、`qq_queue_delay_ms`、`redis_high_pending` 和 `redis_normal_pending` 是 Redis
Stream Dispatcher 落地后的目标时间戳/指标，当前不能作为已经采集的监控项。

核心指标：

```text
presence_detect_to_queue_ms
presence_http_duration_ms
server_state_commit_ms
event_commit_to_sse_ms
sse_receive_to_ui_ms
qq_delivery_duration_ms
sse_first_byte_ms
sse_reconnect_count
sse_cursor_lag
```

### 12.1 星图窗口大小与位置记忆

预警星图窗口需要记住用户最后一次手动调整后的大小和位置。状态保存在当前用户的
`alert_client_state.json`，与告警去重 ID、最后完整处理的 SSE `Last-Event-ID` 游标和星图
账号选择共用同一份本地状态文件，不写入安装目录，也不上传服务端。

保存与恢复规则：

- 用户拖动或缩放结束后立即保存 `x / y / width / height`；
- 客户端正常退出或关闭预警功能时再保存一次，避免遗漏最后一次调整；
- 下次启动和从托盘重新显示时优先恢复保存值，不再强制移动到默认位置；
- 保存尺寸必须遵守星图最小尺寸；
- 多显示器、分辨率或缩放比例变化后，窗口必须被夹取到任一显示器的可用区域内；
- 如果保存位置与当前全部显示器都不相交，则回退到当前 EVE 窗口所在屏幕的右上角默认位置；
- 自动内容布局不得覆盖用户保存的尺寸，只有从未手动调整过的窗口才按内容自动缩放。

## 13. 实施阶段

### 阶段 0：方案冻结

- 固定事件类型、优先级、游标和载荷；
- 固定普通 OCR 与按需 OCR 边界；
- 固定实时性目标和故障隔离要求；
- 本文作为后续变更基线。

### 阶段 1：服务端事件日志

- [x] 新增 `intel_events` 表和索引；
- [x] 实现事件追加、分页和 `state:<seq>` 游标；
- [x] 在 Presence/OCR/过期清理事务中写入进入、更新、清空事件；
- [x] 服务端启动时清理超过 14 天的事件，但永久保留最新序号锚点，避免事件表无限增长且
  保证静默期内水位不回退。

### 阶段 2：SSE 事件重放

- [x] 活跃事件流优先读取 `intel_events`；
- [x] 保留旧事件名兼容映射；
- [x] PostgreSQL 模式删除连接内临时清空推导（内存存储保留兼容回退）；
- [x] 首字节、断线补齐和并发订阅回归测试；
- [x] 星图客户端持久化最高已确认 `state:<sequence>`，并在重连时发送 `Last-Event-ID`；
- [ ] 增加游标过期后的 `cursor_reset` bootstrap。

### 阶段 3：机器人可靠投递

- [x] 机器人识别 `alert.entered`、`alert.updated`、`alert.cleared` 和 `node.updated`；
- [x] 清空事件使用服务端权威事件，不再依赖客户端快照推导；
- [ ] SSE Reader 与 QQ Dispatcher 完全解耦；
- [ ] Redis high/normal/dead stream、重试、死信和 Bootstrap 对账；
- [ ] 队列落地后禁止 QQ API 调用继续运行在 SSE Reader 中。

### 阶段 4：客户端通道拆分

- [x] Presence、OCR、Heartbeat 已由可靠上传管理器分别排队；
- [x] 清空和进入沿用独立 Presence 优先队列；
- [x] 默认视觉扫描间隔为 2 秒，可配置范围 1～10 秒；
- [x] 心跳默认 10 秒、最短 5 秒，并携带全部节点 Presence 对账状态；
- [x] 预警客户端普通事件 Heartbeat 受最短间隔限制，只有错误路径允许强制上报；
- [x] 实现连续两帧零值确认以及 Presence ACK 持久化重试；
- [x] 从普通 OCR 和按需 OCR 载荷中移除 `hostile_icon_count`；
- [ ] 进一步拆分底层网络连接和按需 OCR 的独立重试策略。

### 阶段 5：灰度与清理

- 验证进入、清空、移动、断线、重启和高负载；
- 观察 p50/p95 时延；
- 删除旧的独立移动消息；
- 删除每连接全量扫描和临时 `safe` 推导；
- 修复 PostgreSQL `active_only` SSE 路径，确保读取持久化 `intel_events`；
- 验证黄色、灰色、移除和恢复四类节点状态；
- 验证节点离线不会触发人员清空或敌对清空消息。

### 阶段 6：星图窗口状态

- [x] 保存并恢复星图窗口大小和位置；
- [x] 拖动、缩放结束及客户端退出时持久化；
- [x] 多屏幕、分辨率和 DPI 变化时校正到可见区域；
- [x] 托盘隐藏后重新显示不得覆盖用户位置；
- [x] 增加状态文件往返和屏幕越界回退测试；
- [x] 节点 `health_status` 变化触发账号覆盖层和局部星图刷新。

### 阶段 7：QQ 查询与上线监测

- [x] 支持静态查询按钮模板和无模板降级；生产模板 ID 由环境变量配置；
- [x] 查询菜单、节点敌情和预警节点使用快速数据路径；
- [x] 指定星系 OCR 只下发到父 detector 在线且标记监控的对应星系目标；
- [x] 所有节点查询返回本次完整 OCR 名单；
- [x] 主动 OCR 查询立即确认、后台返回结果并进行 Redis 去重；
- [x] PostgreSQL 持久化人员、军团和联盟上线监测；
- [x] 最低 30 秒调度、全局合并 OCR、完整快照短时复用、重新布防和删除操作。

## 14. 禁止回归

后续修改不得：

- 让 OCR 成为首条敌对预警的前置条件；
- 让 Presence 等待普通 OCR；
- 让客户端用 OCR 名单长度代替服务端 `hostile_count`；
- 让 OCR 请求携带或修改 `hostile_icon_count`；
- 让节点离线或心跳超时伪装成“敌对确认清空”；
- 让第一次心跳缺失的节点继续显示为绿色正常状态；
- 让每个 SSE 连接扫描完整历史报告；
- 让 Bootstrap 取代实时事件；
- 让一次连接重建强制重复播报完整节点列表；
- 让敌对人数变化触发重复的完整节点表格；
- 把上传失败显示成 SSE 连接异常；
- 恢复独立的冗余敌对移动消息；
- 每次显示星图时强制覆盖用户保存的大小或位置；
- 仅用“波次 + 名单指纹”去重人员事件；同一波次内名单可能经过其他状态后再次回到相同组合。

## 15. 验收标准

- 红色图标与 OCR 复用同一截图，且互不阻塞；
- Presence 在默认 2 秒检测周期内立即进入独立上传通道；
- 零值只有连续两帧确认后才上传，服务端收到后立即清空；
- Presence 请求持续重试到 ACK，丢失时由下一次默认 10 秒心跳对账；
- 服务端收到 Presence 后可生成进入或清空事件；
- SSE 断线后能按游标补齐进入和清空；
- 星图首条服务端预警 p95 ≤ 3 秒；
- QQ 首条来敌 p95 ≤ 2 秒（不含 QQ 平台自身不可控延迟）；
- OCR/ESI 失败不影响首条预警；
- QQ 失败不影响星图；
- 查询 OCR 可以在无敌对图标和常规 OCR 关闭时执行；
- OCR 不携带敌对人数，也不能重新激活已清空或离线的节点；
- 第一次缺失心跳时节点变黄，第二次变灰并移除其情报，第三次从列表删除；
- 只有 `health_status` 变化、账号和星系其余字段不变时，星图也立即刷新；
- 节点消息使用包含状态、星系和敌对人数的 Markdown 表格；
- 节点离线、人员名单清空和恢复后零人都不产生额外清空消息；
- 同一事件不会向同一群重复发送；
- 同一波次内人员名单按状态变更序号投递：`A → B → A` 必须播报三次，连续重复的 `A → A` 只播报一次；
- 星图重启后按持久事件 ID 恢复；机器人优先按 Redis 已确认的 `state:<sequence>` 通过
  `Last-Event-ID` 恢复，并以单调 `since` 回退；二者都通过 Bootstrap 对账，统一
  `cursor_reset` 恢复仍属于后续目标；
- 所有关键时延均可通过 `request_id` 和 `event_key` 追踪；
- 星图重启和托盘重新显示后保持上次大小与位置；显示器布局变化后窗口仍完整可见。

## 16. 人员名单回退状态漏报复盘（2026-09-08）

EOY-BG 在同一波次内先后出现 `2 → 3 → 2 → 1` 人变化，最后一次 1 人名单没有播报。服务端 Bootstrap 和机器人 Redis 均已正确保存 1 人状态，SSE 也正常；漏报发生在 QQ 投递去重阶段。

旧去重键由“星系 + 波次 + 名单指纹”组成。该波次更早时已经出现过相同的 1 人名单，因此名单经过其他组合后再次回到 1 人时，旧键仍存在，被误判为重复消息。

修复后在机器人持久化状态中增加波次内 `personnel_revision`：每次名单指纹发生变化即递增，QQ 去重键同时包含变更序号和名单指纹。这样 SSE 重放或连续相同 Bootstrap 仍可去重，而 `A → B → A` 这类真实状态回退能够再次播报。

## 17. 生产推送审计整改台账（2026-09-08）

本节记录客户端、服务端、SSE、星图和 QQ 机器人生产日志审计中发现的问题。后续整改必须更新同一条记录，不能通过删除问题或只修改结论表示完成。

状态定义：

- `待整改`：根因和影响已确认，尚未提交修复；
- `待核实`：已有异常证据，但仍需补齐端到端证据或最终根因；
- `整改中`：已经开始修改，但尚未完成生产验收；
- `待验收`：代码已部署，等待生产日志和真实事件验证；
- `已整改`：代码、测试、部署和生产验收全部完成；
- `持续观察`：当前符合要求，但需要保留监控数据防止回归。

每条问题更新为 `已整改` 时必须填写修复提交、部署时间、生产版本和验收证据。修复提交、
自动测试或 Release 发布均不等于生产安装：客户端修复尚未安装到实际运行环境时保持
`整改中`；完成安装但生产观察尚未结束时标记为 `待验收`；代码、测试、安装和生产证据
全部齐全后才能标记为 `已整改`。

| 编号 | 优先级 | 范围 | 问题与生产证据 | 影响 | 当前状态 | 整改与验收要求 |
|---|---|---|---|---|---|---|
| PUSH-20260908-01 | P0 | 检测/预警客户端心跳 | 心跳设计间隔为 10 秒，但审计时运行的单个 `EVE-Sentry-Monitor.exe` 在约 4 小时内发送 50,510 次心跳；典型突发为约 10 秒连续发送 106 次，相邻请求约 70–110ms。服务端均返回 201，排除失败重试和多客户端进程。审计时将其中一部分异常归因于旧生产安装包中的游戏日志状态联动。 | 抢占 Presence/OCR 上传通道，放大服务端请求量，可能造成连接异常、首条预警延迟和重连连锁反应。 | 整改中 | `abc9028` 已移除游戏掉线检测，`af76ec9` 已为检测客户端上传器增加 Heartbeat 合并及最短线速率；两项都已包含在 `v1.0.64@c8c4e7f`。`8967be7` 进一步取消预警客户端在每条 `alert/safe` 事件后的强制 Heartbeat，并持久化最后完整处理的 SSE `Last-Event-ID`，避免重连重放放大心跳；该提交不属于 v1.0.64，首次包含在 `v1.0.65@3172bb7`。v1.0.65 已发布但尚未安装到验收客户端，因此保持整改中；安装后验证 30 分钟内常规心跳不超过 190 次，并确认重连及事件突发不会形成额外心跳风暴。 |
| PUSH-20260908-02 | P0 | 客户端实现归属 | 历史上仓库同时存在根目录客户端实现和 `client/` 客户端实现，导致修复落点、测试对象与生产构建来源容易混淆。原记录所称“v1.0.64 继续从根目录客户端构建”不准确：v1.0.64 的 Release workflow 和 `eve-sentry-client-source.json` 均证明其从单体仓库 `client/` 构建，目标为 `c8c4e7f`。 | 修复可能落在不参与构建的副本，造成“仓库已修复、发布包仍异常”；重复源码也会使生产包来源难以审计。 | 整改中 | `8967be7` 删除根目录旧客户端树并确立 `client/` 为唯一客户端源码；`3b60a0b` 又移除发布流程对废弃独立客户端仓库的模型回退。上述修复均包含在 `v1.0.65@3172bb7`，发布源码元数据中的 `source_repository`、`release_repository` 均为 `xiaqijun/eve-sentry`，`source_commit` 和 `release_workflow_commit` 均为完整目标 SHA。由于 v1.0.65 尚未安装到验收客户端，保持整改中；安装后检查运行包版本、源码元数据及旧根目录符号均不存在。 |
| PUSH-20260908-03 | P1 | QQ 机器人可靠投递 | Redis 的事件游标持续推进，存在 1,517 个投递去重键，说明机器人并未完全停止消费；但审计时间窗口内缺少 `alert event delivered`、`system transition processed`、`personnel update processed` 等成功日志，无法逐条证明服务端事件已成功投递 QQ。 | 出现漏报或延迟时无法区分 SSE 未消费、Redis 去重、QQ API 失败或日志丢失，可靠性不可审计。 | 待验收 | 已为 SSE 接收/确认日志和各类 QQ 事件处理日志补充 `event_key`、处理结果、失败数；机器人已部署且服务 active，服务端 Bootstrap 检查 HTTP 200。持久队列、ACK/死信仍是后续阶段，待真实敌对事件核验。 |
| PUSH-20260908-04 | P1 | QQ 投递架构 | 当前可确认 Redis 游标和状态已更新，但尚未实现计划中的 SSE Reader 与 QQ Dispatcher 完全解耦，也没有完整的 Redis high/normal/dead stream、失败重试和死信闭环。 | QQ API 短暂失败或投递处理耗时时，仍有阻塞事件消费、延迟后续来敌/清空或丢失失败任务的风险。 | 待整改 | 按阶段 3 实施持久队列、优先级、ACK、指数退避和死信；只有 QQ 成功后才能写投递去重完成标记。断网、限流、机器人重启测试必须证明事件不丢失且同群不重复。 |
| PUSH-20260908-05 | P1 | 人员名单更新 | 最近 6 小时 `alert.updated` 共 57 条，落库延迟 p50 58.5ms、p95 12.76 秒、最大 15.34 秒；首条 Presence 来敌不受影响，但 OCR/ESI 人员补全和名单增减消息可能延迟。 | 首条来敌及时，但人员名单、人员减少和身份修正无法满足接近实时的体验，可能让用户短时间看到过期名单。 | 待验收 | 机器人人员合并窗口已由 10 秒降为 1 秒，保留同一星系短时突发的最新状态合并；机器人测试和配置测试通过，生产服务已重启。待真实事件验证 `alert.updated` 和 QQ 到达 p95。 |
| PUSH-20260908-06 | P1 | 端到端可观测性 | 当前只能分别统计客户端 HTTP、PostgreSQL 事件、SSE 状态和 Redis 游标，不能使用一个稳定标识还原“画面出现 → Presence → 事件提交 → SSE → 星图/机器人 → QQ”的完整时间线。 | 无法可靠计算真实端到端 p50/p95，也无法在漏报时快速确定责任环节。 | 待整改 | Presence 保留 `presence_state_id`，服务端事件保留 `event_key/seq`，机器人和 QQ 日志继续携带同一关联标识；增加按日统计和超标告警。日志不得包含认证信息。 |
| PUSH-20260908-07 | P2 | 机器人 SSE 重连 | 服务端部署重启期间机器人出现 `ConnectError`，并按 0.2/1/3/5 秒退避；之后 Redis 游标恢复推进。当前未发现永久断线或游标停滞。 | 正常部署窗口会产生短暂延迟；若缺少部署后自动对账，仍可能隐藏边界事件遗漏。 | 持续观察 | 保留有界空闲超时、退避重连和 Bootstrap 对账；每次部署后验证机器人游标追平服务端最新事件序号，连续 5 分钟无滞后。 |
| PUSH-20260908-08 | 基线 | 服务端进入/清空 | 最近 6 小时 `alert.entered` 17 条，p95 37.2ms；`alert.cleared` 17 条，p95 52.2ms。当前 SSE 活跃连接约 2 个，无 `CLOSE-WAIT` 堆积。 | 当前服务端状态事务和 SSE 主路径符合实时性要求。 | 持续观察 | 保持事件提交 p95 不超过 300ms；部署后持续检查 SSE 首字节、活动连接数、游标滞后和 PostgreSQL 延迟，禁止恢复每连接历史全量扫描。 |
| PUSH-20260908-09 | P1 | 星图节点健康状态刷新 | 代码审计和回归复现发现，星图账号同步签名原先只比较账号键、星系、星系 ID 和监控开关，没有比较 `health_status`。仅发生 `online → degraded → offline` 时不会更新覆盖层或局部星图。 | 黄色连接异常和灰色离线可能不能及时显示，使用户误判监控节点仍正常。 | 整改中 | `3b60a0b` 已将规范化的 `health_status` 纳入新旧账号签名，并增加 `test_sync_map_accounts_refreshes_when_health_status_changes`。Client CI run `34224776451` 和 Release Client run `34224955352` 均成功，修复已包含在 `v1.0.65@3172bb7`。客户端尚未安装；安装后实测绿、黄、灰、删除及恢复状态在无其他账号变化时也能刷新，并确认不会引起无关节点表重复播报。 |
| PUSH-20260908-10 | P1 | 节点健康状态抖动 | 21:07～22:04 的 QQ 记录中，两个监控窗口多次在同一秒一起显示 `连接异常`，1～3 秒后又一起恢复；它们属于同一个父 detector 心跳。服务端原先对默认 10 秒心跳只留 2 秒宽限，SSE 在 12.01 秒边界主动重算，因此一次 13～15 秒的迟到会生成“双黄 → 双绿”。 | 健康客户端被误报为全部异常/非在线，机器人重复推送完整节点表，掩盖真实故障并造成告警疲劳。 | 待验收 | `9f40b39` 将调度宽限调整为半个心跳周期且最多 5 秒，默认健康边界改为 15/25/35 秒；保留服务端 health/version/SSE 与机器人即时推送语义，并增加默认阈值和同父双节点回归测试。Deploy Server run `34298007470` 已于北京时间 2026-09-09 09:14 成功，生产服务健康。仍需连续观察至少 30 分钟：正常 13～15 秒抖动不得产生黄绿消息，真实越界仍应依次变黄、变灰、删除并可恢复。 |
| PUSH-20260908-11 | P0 | 持久事件与 Bootstrap 一致性 | 服务端事件历史只有 HB-FSO 的 `state:532 alert.entered` 和 `state:533 alert.cleared`，但 QQ 在首组“来敌 → 清空”之后又发送一次“来敌 → 清空”；QQ 顺序为清空、节点表、再来敌。SSE 原先先取得活动缓存、再查询持久事件，clear 在两步之间提交时会输出 `safe → 清空前缓存的 Bootstrap`。 | 机器人把陈旧快照当成新波次，制造并不存在的二次来敌和清空；迟到事件还可能让 Redis 时间游标倒退并扩大重放范围。 | 整改中 | `9f40b39` 已实现 PostgreSQL 单视图水位快照、OCR/ESI 因果事务、状态事件提交前缀、SSE 固定水位重放、零水位恢复、机器人单调游标/幂等 clear，以及桌面客户端最高状态序号保持。Deploy Server run `34298007470` 和 Validate and Deploy Bot run `34298007461` 已成功；客户端修复已由 `7b29e9a` 发布为 v1.0.66，Client CI run `34299474522` 和 Release Client run `34299593162` 均成功。由于 v1.0.66 尚未安装到实际 Windows 客户端，继续保持整改中；安装后核对同一 `event_key` 只产生一组 QQ 进入/清空，并验证断流重连、原生 EventSource 与桌面游标恢复。 |
| PUSH-20260909-12 | P1 | 节点上线推送延迟 | 实测点击开始监控到 QQ 节点上线消息超过 9 秒。客户端虽立即提交上线心跳，但可靠上传器把它与普通周期心跳一起限制为距上次发送至少 10 秒；服务端生产心跳请求通常只耗时约 5～7ms，并会立即唤醒 SSE。 | 刚启动的监控窗口在最长约 10 秒内仍显示离线，降低节点状态和告警覆盖的时效性。 | 待验收 | `cdcadc9` 将上线、下线和连接恢复等显式状态心跳标记为优先任务并绕过周期限速，普通心跳仍保持 10 秒合并限速。新增回归验证状态心跳在 1 秒内进入发送调用；v1.0.67 已发布并安装，用户定性反馈“上线速度挺快”。仍需记录带时间戳的端到端样本后转为已整改。 |
| PUSH-20260909-13 | P1 | 节点下线推送延迟 | 安装 v1.0.67 后，点击停止监控偶发没有 QQ 下线消息，已有消息也超过 4 秒。生产日志显示两次停止操作均先收到两条 `/api/v1/hostile-presence` 清空请求，随后约 7 秒和 5 秒才收到 `/api/v1/clients/heartbeats` 下线心跳；单次服务端处理仅 5～16ms。客户端停止流程先排清空 Presence、后排下线心跳，而可靠上传器又固定让 Presence 优先，形成双重排队。 | 节点停止后仍短暂显示在线，或下线状态被后续心跳覆盖，影响节点可用性判断。 | 待验收 | `4cc46af` 将停止流程改为先提交 `heartbeat:offline`，并让上线、下线、连接恢复等显式状态心跳优先于排队中的 Presence；普通周期心跳仍保持 Presence 优先和 10 秒限速。新增停止入队顺序与队列抢占回归，客户端 425 项测试通过；v1.0.68 已发布。安装后实测“点击停止监控 → QQ 收到节点表”，确认不再包含两条 Presence 的排队时间且无漏发。 |
| PUSH-20260909-14 | P0 | OCR/ESI 全局锁与 SSE 延迟 | 11:11:20～11:11:28 的 `state:550`～`state:553` 已按顺序落库，但 QQ 到 11:11:50～11:11:52 才成批投递；机器人在 11:11:03、11:11:48 两次发生 SSE 空闲超时。补查时服务端报告 23 条 SSE，操作系统只有 3 条真实连接，另有 20 条 `CLOSE_WAIT`；18 个请求线程等待同一个无超时锁，OCR/ESI 后台线程正在网络 `poll`。代码确认异步 OCR 已在锁外取得身份资料后，又在持有 store 全局锁时执行一次可能访问远端 Gateway 的 metadata enrichment。 | 一次 20～40 秒 ESI 等待会同时阻塞 Presence、OCR、心跳和 SSE 快照，造成来敌/清空红标延迟、QQ群消息堆积后集中出现以及客户端显示重连。 | 待验收 | `b1013da` 将可能访问远端 ESI 的 metadata enrichment 完整移到全局锁外，锁内只保留当前状态合并和持久化快照准备，并增加“enrichment 执行时不得持有 store 锁”的回归测试。Deploy Server run `34309736878` 于北京时间 2026-09-09 12:13 成功；重启后由 29 个线程/23 条 SSE/20 条 `CLOSE_WAIT` 回落到 8 个线程/2 条 SSE/0 条 `CLOSE_WAIT`，两个以上完整 SSE 周期均约 30.0 秒结束，部署后未再出现非部署重连。仍需用下一次真实来敌/清空核验端到端到达时间后转为已整改。 |
| PUSH-20260909-15 | P1 | 来敌后误发节点表 | 09:17 和 11:09 的生产样本均出现来敌消息后又发送完整“在线监控节点”表。代码确认 `monitoring_nodes_version` 虽然只保留节点身份、星系和健康状态，却直接按上游列表顺序计算哈希；上游列表使用包含 `hostile_count`、`presence_version` 和 `captured_at` 的完整节点行排序，因此一次 Presence 更新可能改变相同节点集合的排列顺序并产生错误的新版本。机器人将版本变化当作节点状态快照变化，随后发送整张节点表。 | 节点实际没有上线、下线、移动或健康变化，却在来敌/清空过程中插入冗余节点消息，造成消息顺序混乱并让用户误以为节点发生变化。 | 待验收 | `17861e7` 在计算节点版本前按 `node_id + system_name + health_status` 稳定排序，并增加“同一节点集合换序版本不变”以及“敌对人数、Presence 版本和采集时间变化不产生节点变化”的回归。服务端测试 599 项通过、1 项跳过；Contract Compatibility run `34312307206` 和 Deploy Server run `34312307192` 均成功，生产于北京时间 2026-09-09 12:53 切换到该提交，readiness 正常且无 `CLOSE_WAIT`。下一次真实来敌/清空必须只发送预警、人员和清空消息；只有节点上线、下线、移动或健康状态变化才允许发送完整节点表。 |
| PUSH-20260909-16 | P1 | 频繁开关监控时节点推送排队 | 每次真实上线或下线都会生成完整节点快照；机器人原先在 SSE 读取循环中同步等待 QQ Markdown、降级文本和网络重试。频繁开关时，已经进入投递的旧快照不能由客户端心跳合并取消，后续快照及同一 SSE 上的预警事件只能排队等待；QQ 单请求最多 10 秒且默认重试 3 次，会显著放大延迟。 | QQ 群可能先后看到已经过期的在线/离线状态，最新节点状态和后续来敌/清空消息被旧节点投递拖延。 | 整改中 | 机器人现将最新节点快照先持久化到 Redis，再由独立任务投递；默认合并 250ms 突发，发送进行中只保留一份最新待发快照，跳过中间状态；单次节点投递限制为 3 秒，超时后优先发送更新快照，无更新时重试当前最新状态。新增慢 QQ、连续三次切换只发送首尾状态，以及旧投递超时后立即发送最新状态的回归。完整 Redis high/normal/dead stream 仍按 `PUSH-20260908-04` 后续实施。部署后需实测快速开关至少 10 轮，确认节点消息不形成 FIFO 积压、最终状态正确且预警事件不被节点消息阻塞。 |
| PUSH-20260909-17 | P1 | 快速重开监控的上线延迟 | 生产样本中客户端于 15:02:08 点击开启，服务端直到 15:02:18 才看到节点上线，恰好相隔一个 10 秒周期心跳。代码确认 `_start_monitor` 启动工作线程后立即构造上线心跳，但此时 `QThread.isRunning()` 可能仍为 false，导致首包被误标为 `monitoring=false`；下一次周期心跳才纠正为在线。 | 关闭后立即开启时，客户端界面已显示监控中，但星图和 QQ 节点状态仍可能延迟约 10 秒。 | 整改中 | `5125409` 让启动路径使用 `monitoring_override=true` 构造首个 `heartbeat:online`，不再依赖线程调度时序，并增加启动首包回归。修复已发布为 v1.0.69；安装后连续快速关闭/开启至少 10 轮，记录客户端点击、服务端心跳和 QQ 到达时间，确认不再出现整周期等待。 |
| PUSH-20260909-18 | P2 | 空监控节点列表排版 | 节点快照为空时，机器人原先只输出标题和一行普通文本；非空时则输出四列表格，QQ Markdown 在两种结构间切换会造成空状态排版错乱。 | 全部节点下线时，群内节点状态消息难以阅读，且与正常节点表的列结构不一致。 | 待验收 | `5125409` 让空快照保留节点、状态、星系和敌对人数四列表头，并使用“暂无在线监控节点”占位行；增加精确 Markdown 回归。Validate and Deploy Bot run `34325517223` 已成功，等待下一次真实全下线消息确认 QQ 客户端显示正常。 |

### 17.1 整改更新格式

整改一项问题时，在对应表格状态中按 `整改中 → 待验收 → 已整改` 更新，并在表格后追加记录：

```text
问题编号：PUSH-20260908-XX
修复提交：<commit>
部署时间：<北京时间>
生产版本：<release/commit>
自动测试：<测试范围与结果>
生产验收：<观察区间、事件样本、p50/p95、是否漏报/重报>
遗留风险：<无或具体内容>
```

在全部 P0/P1 问题标记为 `已整改` 前，不得将整条推送链路结论写为“已经完全满足及时性和可靠性要求”。

### 17.2 节点表紧跟来敌消息的生产样本

2026-09-08 09:17:44 至 09:18:36 的生产样本：

```text
09:17:44  S-KSWL hostile-presence 到达服务端，HTTP 200，20.453ms
09:17:44  OCR snapshot 到达服务端，HTTP 201，59.011ms
09:17:48  两次客户端 heartbeat 返回 201，但耗时 2374.929ms、4184.573ms
09:17:51  QQ 推送在线监控节点表，其中 S-KSWL 敌对人数为 1
09:17:54  QQ 推送敌对人员名单
09:18:36  QQ 推送 S-KSWL 清空
```

该样本确认：节点表并非由 `alert.entered` 直接调用，而是与心跳后的节点状态快照/Bootstrap 同步发生；但它在来敌消息之后 7 秒发送，用户会感知为来敌触发了冗余节点播报。后续代码检查定位到节点版本的顺序稳定性缺陷：版本内容本身不包含敌对人数，但参与版本计算的节点列表顺序来自包含敌对人数、Presence 版本和采集时间的完整行排序；这些易变字段更新后，相同节点集合可能换序并产生不同哈希。该缺陷已由 `17861e7` 修复并记录为 `PUSH-20260909-15`。

该样本并入 `PUSH-20260908-01`、`PUSH-20260908-03`、`PUSH-20260908-06` 和 `PUSH-20260909-15` 的验收证据。整改后的验收必须证明：敌对人数变化只更新敌对事件和星图上的实时人数，不单独触发完整节点表；只有节点上线、离线、移动或健康状态真正变化时才发送节点表。

### 17.3 v1.0.65 发布证据

本节只证明 Release 构建、签名和发布完成，不代表客户端已经安装或完成生产验收。

- Release：[v1.0.65](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.65)
- 目标提交：`3172bb7fffe359c4414fc4566238e5b4ba6d9cc0`
- 发布时间：`2026-09-08T12:19:15Z`（北京时间 `2026-09-08 20:19:15`）
- Client CI：[run 34224776451](https://github.com/xiaqijun/eve-sentry/actions/runs/34224776451)，成功
- Release Client：[run 34224955352](https://github.com/xiaqijun/eve-sentry/actions/runs/34224955352)，成功
- Release target、远端 tag、`source_commit` 和 `release_workflow_commit` 均指向上述完整 SHA。

| 资产 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `EVE-Sentry-Monitor-ONNX-program-1.0.65.zip` | 132,400,793 | `4d4c957b634979e44257de6a450ebc976bb7fc62ff077e455d58c218ed632f79` |
| `EVE-Sentry-Monitor-ONNX-models-eb1a177a0f6e7133c001d4284890844f18b6f1f732b29ccc4307fa5f7364ea2d.zip` | 105,099,126 | `46e115f8b941a93d430eee27d6a68e994e79d9a05ec3f8b03f1d318f9071e132` |
| `latest.json` | 1,276 | `36fb59d4431dd9bb1248a191c7ccf9ce8597bfdadb285079bcd96f8b5d3db4c8` |
| `EVE-Sentry-Monitor-ONNX-1.0.65.zip` | 237,500,101 | `cfc523404a90e1d43a3c901a5a49324392047c3382e019118a3fe52a7509008c` |
| `EVE-Sentry-Channel-1.0.65.zip` | 60,244,877 | `1038e6a86459257fda075ccd1191697e91ba2311c4b0f848aae17738639d8ea0` |
| `eve-sentry-client-source.json` | 260 | `c986ec66f66e9582508d0db3638d313a938952db9f759e951ad26e53b45778cd` |

`latest.json` 内记录的程序包和模型包 SHA-256、大小与上表一致，并已使用客户端内置公钥
完成 Ed25519 签名验证；下载站公开的 `latest.json` 与 Release 附件逐字节一致。

### 17.4 21:00～22:30 生产日志补查

使用生产机密钥直接读取北京时间 2026-09-08 21:00～22:30 的 systemd journal。三项服务
`eve-sentry`、`eve-risk-analysis-bot`、`eve-risk-analysis-worker` 均无重启；服务端共记录
1,181 条 HTTP 请求，其中 451 条 200、724 条 201、5 条 401、1 条 403，没有 5xx。
401/403 均为未授权访问，不是已认证客户端失败。

关键时间线与 QQ 重复消息一致：

```text
21:45:21  OCR snapshot 返回 200，耗时 10983.978ms
21:45:44  两路客户端 heartbeat 返回 201，耗时 6523.017ms、7080.005ms
21:45:52  QQ 机器人 SSE 空闲读取 TimeoutError，0.2 秒后重连
```

该样本证明 OCR/ESI 慢请求曾与心跳延迟同时出现，也证明重复“来敌 → 清空”前发生了 SSE
断流重连。`9f40b39` 缩短了主要数据库/ESI 路径的全局锁持有时间，并让重连从持久状态
序号恢复；2026-09-09 的后续补查仍发现一处 OCR metadata enrichment 在持锁区访问远端
Gateway，已由 `b1013da` 修复并记录为 `PUSH-20260909-14`。

### 17.5 v1.0.66 发布证据

本节只证明代码部署、Release 构建、签名和发布完成，不代表 v1.0.66 已安装到实际客户端
或完成生产验收。

- 修复提交：`9f40b39c2f20153852cb910795d8d2a2f2a440fc`
- Deploy Server：[run 34298007470](https://github.com/xiaqijun/eve-sentry/actions/runs/34298007470)，成功；北京时间 2026-09-09 09:14 完成
- Validate and Deploy Bot：[run 34298007461](https://github.com/xiaqijun/eve-sentry/actions/runs/34298007461)，成功；北京时间 2026-09-09 09:10 完成
- Release：[v1.0.66](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.66)
- 目标提交：`7b29e9aab76e5fa3f2f3cfb18b7c86582aa09f5f`
- 发布时间：`2026-09-09T01:36:39Z`（北京时间 `2026-09-09 09:36:39`）
- Client CI：[run 34299474522](https://github.com/xiaqijun/eve-sentry/actions/runs/34299474522)，成功
- Release Client：[run 34299593162](https://github.com/xiaqijun/eve-sentry/actions/runs/34299593162)，成功
- `source_repository`、`release_repository` 均为 `xiaqijun/eve-sentry`，`source_commit` 和 `release_workflow_commit` 均指向上述完整目标提交。

| 资产 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `EVE-Sentry-Monitor-ONNX-program-1.0.66.zip` | 132,398,515 | `4e8ed4084c2a602003f83424d89950d208b6912f3ceccf6ddd885e973f39bdc0` |
| `EVE-Sentry-Monitor-ONNX-models-eb1a177a0f6e7133c001d4284890844f18b6f1f732b29ccc4307fa5f7364ea2d.zip` | 105,099,126 | `1c1b5d9de248abc3a52d3f99239983b1c9edf3d8bf2810771602524e025028c2` |
| `latest.json` | 1,276 | `1057e55943bae40fb08b6ba1d8b77ae16cf26c243ffdd9dfbac2b9c5d628da1d` |
| `EVE-Sentry-Monitor-ONNX-1.0.66.zip` | 237,497,823 | `f85212e27a42d80ff5d22d18cf2b54dd03c08720d4080aea1aede3b864d06d9d` |
| `EVE-Sentry-Channel-1.0.66.zip` | 60,243,477 | `bc008b39ac169c6d49fb0650737d0398f89ca7a6e5765d58d10afe211c02e763` |
| `eve-sentry-client-source.json` | 260 | `d827d762f0d656d42fc6bfa8058a03d4d778e0bf42d12d63b2a7704f7767c1a9` |

下载站公开 `latest.json` 已切换为 v1.0.66，程序包与模型包名称、大小、SHA-256 均与
Release 资产一致；清单使用 `ed25519` 和 `eve-sentry-release-v1` 签名标识。

### 17.6 v1.0.67 节点上线延迟修复

- 修复提交：`cdcadc93c6e71425d8c1196dc9750618d768d17d`
- Client CI：[run 34304267198](https://github.com/xiaqijun/eve-sentry/actions/runs/34304267198)，成功
- Release Client：[run 34304365145](https://github.com/xiaqijun/eve-sentry/actions/runs/34304365145)，成功
- Release：[v1.0.67](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.67)
- 发布时间：`2026-09-09T02:47:46Z`（北京时间 `2026-09-09 10:47:46`）
- 目标提交：`cdcadc93c6e71425d8c1196dc9750618d768d17d`

| 资产 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `EVE-Sentry-Monitor-ONNX-program-1.0.67.zip` | 132,401,480 | `a6d4a7b62e0ae75db6bc9f5f4b5da53fba6c234060133972f152b7d99e1c961b` |
| `EVE-Sentry-Monitor-ONNX-models-eb1a177a0f6e7133c001d4284890844f18b6f1f732b29ccc4307fa5f7364ea2d.zip` | 105,099,126 | `629bd15290c4ba477ed2fdc8ecc55931166edfe341a97ca821da6d9d7fc7fb7d` |
| `latest.json` | 1,276 | `ac68f41cba3732b79a6165b44559c8e6597b4f09f5719ca6979d123ca2fb3567` |
| `EVE-Sentry-Monitor-ONNX-1.0.67.zip` | 237,500,788 | `08ca8e56928dac3d0cbd65278b586de5caf778a0c31c0761214e38b606b6beea` |
| `EVE-Sentry-Channel-1.0.67.zip` | 60,244,927 | `e92fdcccda9fe225c47a226382b08fa195fbd5f8b811c0e5a46ac138070487ce` |
| `eve-sentry-client-source.json` | 260 | `b201f3e57da1b27e998c4cabe79563a1910770fa72a86d3fb4e78c3739ac7c50` |

Release Client 的 `actions/cache/restore@v6` 与 `actions/cache/save@v6` 已在本次真实发布中
执行成功，客户端程序包、模型包与组合包均已生成并上传。生产验收仍以安装 v1.0.67 后的
真实端到端节点上线推送时间为准。

### 17.7 v1.0.68 节点下线延迟修复

- 修复提交：`4cc46afd33fafca48503c2fe01b12ce80560bf3c`
- Client CI：[run 34305837099](https://github.com/xiaqijun/eve-sentry/actions/runs/34305837099)，成功
- Release Client：[run 34305943975](https://github.com/xiaqijun/eve-sentry/actions/runs/34305943975)，成功
- Release：[v1.0.68](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.68)
- 发布时间：`2026-09-09T03:12:59Z`（北京时间 `2026-09-09 11:12:59`）
- 目标提交：`4cc46afd33fafca48503c2fe01b12ce80560bf3c`

| 资产 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `EVE-Sentry-Monitor-ONNX-program-1.0.68.zip` | 132,401,917 | `679cb870637742bb8d68c4eb0314a0052ec94223494f2a3c920a9e1106afd6bf` |
| `EVE-Sentry-Monitor-ONNX-models-eb1a177a0f6e7133c001d4284890844f18b6f1f732b29ccc4307fa5f7364ea2d.zip` | 105,099,126 | `d597cf6ece6dd86384320d3e2aaded47a0f4eb90d2e8da226705e5d7d35893e3` |
| `latest.json` | 1,276 | `6ee18fd8229564b1253b0d70dd24ec861481cdb50fcb4a3162aaa4097ff56e2c` |
| `EVE-Sentry-Monitor-ONNX-1.0.68.zip` | 237,501,225 | `524ffb3d43dbc26f1eff5fc4f4e26e720fc0970f4c2ffb4b6539461afec495d1` |
| `EVE-Sentry-Channel-1.0.68.zip` | 60,245,143 | `98ecf2b63b69235de268b7c2342bc399b98a32cf12e3c02a49220dfd30bbed93` |
| `eve-sentry-client-source.json` | 260 | `839fa977f29b5a126b20d04f8516b8af48710499177e907ea28a834630beae2e` |

发布任务的模型缓存恢复和保存步骤均成功；客户端程序包、模型包与组合包均已上传。下载站
公开 `latest.json` 已切换为 v1.0.68，其 SHA-256 与 Release 资产一致，`/download/latest`
重定向到 `EVE-Sentry-Monitor-ONNX-1.0.68.zip`。生产验收仍以安装 v1.0.68 后的真实下线
推送时间和无漏发观察为准。

### 17.8 OCR/ESI 持锁阻塞修复

- 问题编号：`PUSH-20260909-14`
- 修复提交：`b1013dad619a149586e5400edb3107d8064e9e54`
- Deploy Server：[run 34309736878](https://github.com/xiaqijun/eve-sentry/actions/runs/34309736878)，成功；北京时间 2026-09-09 12:13 完成
- Contract Compatibility：[run 34309736953](https://github.com/xiaqijun/eve-sentry/actions/runs/34309736953)，成功
- 自动测试：本地服务端全量 `597 passed, 1 skipped`；CI 质量、Windows 服务端/前端验证和 PostgreSQL 集成测试全部通过

生产事件和投递时间线：

```text
11:11:20  state:550 alert.cleared 提交
11:11:24  state:551 alert.entered 发生
11:11:25  state:551 alert.entered 提交
11:11:26  state:552 alert.updated 提交
11:11:28  state:553 alert.cleared 提交
11:11:48  QQ 机器人 SSE 空闲读取 TimeoutError
11:11:50  QQ 才投递 state:550 清空
11:11:51  QQ 才投递 state:551 来敌和人员
11:11:52  QQ 才投递 state:553 清空和节点表
```

事件序号说明真实状态曾在清空后约 4 秒短暂重新进入，并非 QQ 自行打乱 durable event 顺序；
异常之处是服务器持锁导致 SSE 断流，使已经提交的事件延迟 23～30 秒后集中到达。部署前
健康接口显示 23 条 SSE，系统连接为 3 条 `ESTAB`、20 条 `CLOSE_WAIT`，进程共有 29 个
线程；部署后稳定为 2 条 SSE、无 `CLOSE_WAIT`、8 个线程。部署后的 SSE 请求均在预期的
30 秒窗口结束，常规心跳约 5～7ms，观察窗口内未再出现 bot `TimeoutError`。本条仍为
`待验收`，因为最终结论需要下一次真实敌对进入和清空同时证明预警端红标与 QQ 到达时延。

### 17.9 v1.0.69 快速重开与空节点排版修复

- 问题编号：`PUSH-20260909-17`、`PUSH-20260909-18`
- 修复提交：`51254096ce09e4c70093a811adfca79289ef946f`
- Client CI：[run 34325517224](https://github.com/xiaqijun/eve-sentry/actions/runs/34325517224)，成功
- Contract Compatibility：[run 34325517222](https://github.com/xiaqijun/eve-sentry/actions/runs/34325517222)，成功
- Validate and Deploy Bot：[run 34325517223](https://github.com/xiaqijun/eve-sentry/actions/runs/34325517223)，成功
- Release Client：[run 34325665371](https://github.com/xiaqijun/eve-sentry/actions/runs/34325665371)，成功
- Release：[v1.0.69](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.69)
- 发布时间：`2026-09-09T07:51:37Z`（北京时间 `2026-09-09 15:51:37`）
- 目标提交：`51254096ce09e4c70093a811adfca79289ef946f`

| 资产 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `EVE-Sentry-Monitor-ONNX-program-1.0.69.zip` | 132,401,667 | `9ce7528773c3edd11acb2576040fdcaab009d6df73379a18c604059b7e460cf9` |
| `EVE-Sentry-Monitor-ONNX-models-eb1a177a0f6e7133c001d4284890844f18b6f1f732b29ccc4307fa5f7364ea2d.zip` | 105,099,126 | `c125fa393aa62f6d880c128459d3416c38db90dc42124bd7dd3f0417e6d82a19` |
| `latest.json` | 1,276 | `92b66a0fd5afe6c34bebcd9f601e0d0af2ec2c68e53a75f435339806356aacf6` |
| `EVE-Sentry-Monitor-ONNX-1.0.69.zip` | 237,500,975 | `125bbbe8dce22c82fba6e03688f32d38ee6c1a20f26bb518fe76ec78f63fa0b1` |
| `EVE-Sentry-Channel-1.0.69.zip` | 60,245,339 | `2add28827268a618e78084ee46708fceba7839a6b922f870afbf53633c8b9b1b` |
| `eve-sentry-client-source.json` | 260 | `2c5ac983755df8fd8626ea6caa515044987e7ed89827ffc589b1a500ed6562c7` |

本地客户端普通测试 355 项、client-server 集成测试 70 项、工作流安全测试 8 项通过；
机器人全量测试通过。生产 `latest.json` 与 Release 附件逐字节一致，Ed25519 签名验证
成功，`/download/latest` 返回 302 并指向 v1.0.69 组合包，固定下载支持 Range 206。
`eve-sentry-client-source.json` 的 `source_commit` 和 `release_workflow_commit` 均指向上述
完整目标提交。客户端快速重开仍需安装 v1.0.69 后完成真实端到端验收。
