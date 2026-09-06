# 可靠推送与按需 OCR 实施计划

本文是服务端、监控客户端、预警客户端和 QQ 机器人共用的推送方案。后续涉及
Presence、OCR、Heartbeat、SSE、告警、清空、节点或按需 OCR 的修改，都必须先对照本文。

## 1. 结论

整体流程没有概念性冲突，关键边界已经确认：

- 红色敌对图标检测和 OCR 使用同一张截图的两条分支，不是先检测后重新截图；
- Presence 是实时预警依据，必须先于 OCR、ESI、zKill 和人员名单补全；
- 普通 OCR 是客户端自动补充名单；
- 按需 OCR 是 QQ 查询触发的一次性 OCR，不依赖红色图标，也不受常规 OCR 开关影响；
- Bootstrap 只负责初始化、重连恢复和周期性对账，不承担实时事件生成；
- 清空事件必须由服务端状态变化生成，不能由每个 SSE 连接临时推导；
- 星图客户端和 QQ 机器人都消费服务端权威事件，但 QQ 的投递失败不能影响星图；
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

### 3.2 普通 OCR

普通 OCR 由监控线程根据状态变化自动触发：

- 敌对数量变化；
- 名单区域画面变化；
- OCR 短时重试；
- 常规监控需要补充人员名单。

普通 OCR 只提供增效信息，失败不能撤销 Presence，也不能把系统直接判定为清空。

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

实时性规则：

- P0 进入、清空和数量变化不经过合并窗口；
- P1 人员和身份更新最多延迟 200～300ms；
- 节点更新最多延迟 300～500ms；
- Bootstrap 每 30 秒对账，不作为正常实时推送路径。

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
  └─ 查询命令

SSE Reader
  └─ 接收服务端事件
```

Presence 不能等待 OCR、Heartbeat 或已经开始的普通上传请求。清空 `0` 与非零进入拥有
相同优先级。客户端界面必须分别显示 SSE 状态和上传状态，OCR 失败不能显示为 SSE 连接异常。

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
- [ ] 进一步拆分底层网络连接和按需 OCR 的独立重试策略。

### 阶段 5：灰度与清理

- 验证进入、清空、移动、断线、重启和高负载；
- 观察 p50/p95 时延；
- 删除旧的独立移动消息；
- 删除每连接全量扫描和临时 `safe` 推导。

## 14. 禁止回归

后续修改不得：

- 让 OCR 成为首条敌对预警的前置条件；
- 让 Presence 等待普通 OCR；
- 让 QQ API 调用运行在 SSE Reader 中；
- 让客户端用 OCR 名单长度代替服务端 `hostile_count`；
- 让每个 SSE 连接扫描完整历史报告；
- 让 Bootstrap 取代实时事件；
- 让一次连接重建强制重复播报完整节点列表；
- 把上传失败显示成 SSE 连接异常；
- 恢复独立的冗余敌对移动消息。

## 15. 验收标准

- 红色图标与 OCR 复用同一截图，且互不阻塞；
- Presence 在默认 1 秒检测周期内立即进入独立上传通道；
- 服务端收到 Presence 后可生成进入或清空事件；
- SSE 断线后能按游标补齐进入和清空；
- 星图首条服务端预警 p95 ≤ 2 秒；
- QQ 首条来敌 p95 ≤ 2 秒（不含 QQ 平台自身不可控延迟）；
- OCR/ESI 失败不影响首条预警；
- QQ 失败不影响星图；
- 查询 OCR 可以在无敌对图标和常规 OCR 关闭时执行；
- 同一事件不会向同一群重复发送；
- 客户端、服务端、机器人重启后可通过 Bootstrap 对账；
- 所有关键时延均可通过 `request_id` 和 `event_key` 追踪。
