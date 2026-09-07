# 可靠推送与按需 OCR 实施计划

本文是服务端、监控客户端、预警客户端和 QQ 机器人共用的推送方案。后续涉及
Presence、OCR、Heartbeat、SSE、告警、清空、节点或按需 OCR 的修改，都必须先对照本文。

## 1. 结论

整体流程没有概念性冲突，关键边界已经确认：

- 红色敌对图标检测和 OCR 使用同一张截图的两条分支，不是先检测后重新截图；
- Presence 是实时预警和地图敌对人数的唯一权威依据，必须先于 OCR、ESI、zKill 和人员名单补全；
- OCR 只上传人员名单，不携带 `hostile_icon_count`，也不得创建、刷新或清空 Presence；
- 普通 OCR 是客户端自动补充名单；
- 按需 OCR 是 QQ 查询触发的一次性 OCR，不依赖红色图标，也不受常规 OCR 开关影响；
- 客户端连续两帧检测为零后，只发送一条权威的 Presence 清空；
- 心跳固定为 10 秒，并携带全部监控节点的最新 Presence 状态用于丢包对账；
- Bootstrap 只负责初始化、重连恢复和周期性对账，不承担实时事件生成；
- 清空事件必须由服务端状态变化生成，不能由每个 SSE 连接临时推导；
- 星图客户端和 QQ 机器人都消费服务端权威事件，但 QQ 的投递失败不能影响星图；
- 节点状态采用第一次缺失黄色、第二次缺失灰色、第三次缺失移除；
- 节点离线导致的情报移除不播报为敌对清空，也不发送人员清空消息；
- WebSocket 不是当前必需，继续使用 HTTP 上传 + SSE 下行。

当前版本已落地第一批可靠性改造：PostgreSQL `intel_events` 事件表、进入/更新/清空事件
的事务内追加、SSE 首字节立即返回及事件游标重放、机器人对新事件名的兼容处理，以及客户端
默认 1 秒视觉扫描间隔。Redis Stream 投递队列和客户端上传通道的完全拆分仍属于后续阶段。

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
  → SSE 立即发送 alert.entered / alert.cleared / alert.updated
  → 星图客户端立即更新
  → QQ 机器人写入 Redis high/normal stream
  → QQ Dispatcher 投递消息

OCR Normal Lane
  → POST /api/v1/ocr/snapshot
  → 服务端异步解析角色、军团、联盟、standings
  → 生成 alert.updated
  → 星图和 QQ 更新人员名单

Heartbeat Reconciliation Lane（每 10 秒）
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

## 4. 服务端事件模型

服务端是敌我分类、人员名单和波次状态的唯一权威来源。

### 4.1 事件类型

| 事件 | 触发条件 | 优先级 | 说明 |
| --- | --- | --- | --- |
| `alert.entered` | 系统从 0 变为大于 0 | P0 | 立即发送，不等待 OCR |
| `alert.cleared` | 系统从大于 0 变为 0 | P0 | 必须持久化，不能由 SSE 推导 |
| `alert.updated` | 数量、身份或确认名单变化 | P1 | 允许短窗口合并 |
| `node.updated` | 节点上线、下线、换星系 | P1 | 发送完整节点快照 |

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
| 画面出现 → 客户端检测 | p95 ≤ 1 秒 |
| 检测 → Presence 上传 | p95 ≤ 300ms |
| 服务端收到 → 事件提交 | p95 ≤ 300ms |
| 事件提交 → SSE 发出 | p95 ≤ 300ms |
| SSE 到达 → 星图显示 | p95 ≤ 100ms |
| 画面出现 → 星图服务端预警 | p95 ≤ 2 秒 |
| 服务端收到 → QQ 首条来敌 | p95 ≤ 2 秒 |
| 敌对消失 → 客户端两帧确认 | p95 ≤ 2 秒 |
| 两帧确认 → 星图清空 | p95 ≤ 1 秒 |

实时性规则：

- P0 进入、清空和数量变化不经过合并窗口；
- P1 人员和身份更新最多延迟 200～300ms；
- 节点更新最多延迟 300～500ms；
- Bootstrap 每 30 秒对账，不作为正常实时推送路径。

节点心跳固定为 10 秒。连续缺失的状态时间线为：第一次约 10 秒进入连接异常，第二次约
20 秒进入离线并移除该节点贡献的情报，第三次约 30 秒从节点列表删除。服务端可保留很小的
调度容差，但不得把一次正常的定时器延迟累计成多个缺失次数。

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
如果实时请求丢失，下一次 10 秒心跳使用相同状态完成对账，因此不再单独增加周期 Presence
请求。

建议扫描策略：默认 1 秒检测红色图标；OCR 继续异步执行；同一节点只保留最新 OCR 快照。

## 7. SSE 与游标

SSE 建立后必须立即发送 `: connected`，然后按 `seq` 读取事件：

```text
建立连接
  → connected
  → 读取 Last-Event-ID
  → bootstrap（初始化/对账）
  → 读取 seq > cursor 的事件
  → 5 秒 keepalive
```

要求：

- SSE 事件按 `seq` 顺序发送；
- `id` 使用 `seq`，`event_key` 用于幂等；
- 断线重连从最后确认的游标继续；
- 游标过期时发送 `cursor_reset` 和最新 bootstrap；
- PostgreSQL 模式下无论 `active_only` 是否启用，都必须读取持久化 `intel_events`；
- `active_only` 只能限制历史报告，不能过滤 `alert.cleared`、`node.updated` 等状态事件；
- `clear_reason=node_offline` 的清理事件仍需发送给星图用于移除陈旧状态，但机器人不得将其
  格式化为敌对清空消息；
- 不得在每个 SSE 连接中调用无界的活动告警扫描；
- 不得根据前后快照临时生成清空事件。

兼容期间保留映射：

```text
alert.entered → alert
alert.cleared → safe
node.updated  → monitoring_node
```

## 8. 星图预警客户端

星图客户端收到 SSE 后只做轻量处理：

```text
读取 → 解析 → 提交 UI 信号 → 更新星图/浮窗/声音
```

P0 事件不能等待 Bootstrap、OCR、ESI 或 zKill。客户端每 30 秒执行 Bootstrap 对账，重连后
也必须用 Bootstrap 校正状态，但不能因为 Bootstrap 慢而阻塞实时事件处理。

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

## 9. QQ 机器人

机器人拆为三个组件：

```text
SSE Reader → Redis Stream Writer → QQ Dispatcher
```

SSE Reader 不调用 QQ API。事件写入 Redis 成功后才推进游标。

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
- QQ 失败不能阻塞 SSE Reader，也不能影响星图。

机器人每 30 秒执行 Bootstrap 对账，发现漏报时生成幂等补偿任务。

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
| QQ API 慢或失败 | 星图和 SSE；消息进入重试队列 |
| Redis 重启 | 通过 Consumer Group 恢复 Pending |
| SSE 断线 | 客户端按游标重连并补齐事件 |
| PostgreSQL 暂不可用 | 快速失败，不持有全局锁；客户端保留最新状态 |
| 客户端截图失败 | 上报 capture 状态并清除对应 Presence |
| 单次心跳缺失 | 星图节点变黄、机器人表格显示连接异常；暂不清除情报 |
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
qq_queued_at
qq_delivered_at
```

核心指标：

```text
presence_detect_to_queue_ms
presence_http_duration_ms
server_state_commit_ms
event_commit_to_sse_ms
sse_receive_to_ui_ms
qq_queue_delay_ms
qq_delivery_duration_ms
sse_first_byte_ms
sse_reconnect_count
sse_cursor_lag
redis_high_pending
redis_normal_pending
```

### 12.1 星图窗口大小与位置记忆

预警星图窗口需要记住用户最后一次手动调整后的大小和位置。状态保存在当前用户的
`alert_client_state.json`，与告警去重游标和星图账号选择共用同一份本地状态文件，不写入
安装目录，也不上传服务端。

保存与恢复规则：

- 用户拖动或缩放结束后立即保存 `x / y / width / height`；
- 客户端正常退出或关闭预警功能时再保存一次，避免遗漏最后一次调整；
- 下次启动和从托盘重新显示时优先恢复保存值，不再强制移动到默认位置；
- 保存尺寸必须遵守星图最小尺寸；
- 多显示器、分辨率或缩放比例变化后，窗口必须被夹取到任一显示器的可用区域内；
- 如果保存位置与当前全部显示器都不相交，则回退到当前 EVE 窗口所在屏幕的右上角默认位置；
- 自动内容布局不得覆盖用户保存的尺寸，只有从未手动调整过的窗口才按内容自动缩放。

## 13. 实施阶段

### 阶段 0：方案冻结（当前）

- 固定事件类型、优先级、游标和载荷；
- 固定普通 OCR 与按需 OCR 边界；
- 固定实时性目标和故障隔离要求；
- 本文作为后续变更基线。

### 阶段 1：服务端事件日志

- [x] 新增 `intel_events` 表和索引；
- [x] 实现事件追加、分页和 `state:<seq>` 游标；
- [x] 在 Presence/OCR/过期清理事务中写入进入、更新、清空事件；
- [x] 服务端启动时清理超过 14 天的事件，避免事件表无限增长。

### 阶段 2：SSE 事件重放

- [x] 活跃事件流优先读取 `intel_events`；
- [x] 保留旧事件名兼容映射；
- [x] PostgreSQL 模式删除连接内临时清空推导（内存存储保留兼容回退）；
- [x] 首字节、断线补齐和并发订阅回归测试；
- [ ] 增加游标过期后的 `cursor_reset` bootstrap。

### 阶段 3：机器人可靠投递

- [x] 机器人识别 `alert.entered`、`alert.updated`、`alert.cleared` 和 `node.updated`；
- [x] 清空事件使用服务端权威事件，不再依赖客户端快照推导；
- [ ] SSE Reader 与 QQ Dispatcher 完全解耦；
- [ ] Redis high/normal/dead stream、重试、死信和 Bootstrap 对账。

### 阶段 4：客户端通道拆分

- [x] Presence、OCR、Heartbeat 已由可靠上传管理器分别排队；
- [x] 清空和进入沿用独立 Presence 优先队列；
- [x] 默认视觉扫描间隔调整为 1 秒；
- [x] 将心跳间隔固定为 10 秒并携带全部节点 Presence 对账状态；
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
- [x] 增加状态文件往返和屏幕越界回退测试。

## 14. 禁止回归

后续修改不得：

- 让 OCR 成为首条敌对预警的前置条件；
- 让 Presence 等待普通 OCR；
- 让 QQ API 调用运行在 SSE Reader 中；
- 让客户端用 OCR 名单长度代替服务端 `hostile_count`；
- 让 OCR 请求携带或修改 `hostile_icon_count`；
- 让节点离线或心跳超时伪装成“敌对确认清空”；
- 让第一次心跳缺失的节点继续显示为绿色正常状态；
- 让每个 SSE 连接扫描完整历史报告；
- 让 Bootstrap 取代实时事件；
- 让一次连接重建强制重复播报完整节点列表；
- 让敌对人数变化触发重复的完整节点表格；
- 把上传失败显示成 SSE 连接异常；
- 恢复独立的冗余敌对移动消息。
- 每次显示星图时强制覆盖用户保存的大小或位置。

## 15. 验收标准

- 红色图标与 OCR 复用同一截图，且互不阻塞；
- Presence 在默认 1 秒检测周期内立即进入独立上传通道；
- 零值只有连续两帧确认后才上传，服务端收到后立即清空；
- Presence 请求持续重试到 ACK，丢失时由下一次 10 秒心跳对账；
- 服务端收到 Presence 后可生成进入或清空事件；
- SSE 断线后能按游标补齐进入和清空；
- 星图首条服务端预警 p95 ≤ 2 秒；
- QQ 首条来敌 p95 ≤ 2 秒（不含 QQ 平台自身不可控延迟）；
- OCR/ESI 失败不影响首条预警；
- QQ 失败不影响星图；
- 查询 OCR 可以在无敌对图标和常规 OCR 关闭时执行；
- OCR 不携带敌对人数，也不能重新激活已清空或离线的节点；
- 第一次缺失心跳时节点变黄，第二次变灰并移除其情报，第三次从列表删除；
- 节点消息使用包含状态、星系和敌对人数的 Markdown 表格；
- 节点离线、人员名单清空和恢复后零人都不产生额外清空消息；
- 同一事件不会向同一群重复发送；
- 客户端、服务端、机器人重启后可通过 Bootstrap 对账；
- 所有关键时延均可通过 `request_id` 和 `event_key` 追踪。
- 星图重启和托盘重新显示后保持上次大小与位置；显示器布局变化后窗口仍完整可见。
