# ADR-0001：四仓库线协议边界与兼容策略

- 状态：Proposed
- 日期：2026-08-30
- 范围：`eve-sentry`、`eve-sentry-client`、`eve-sentry-esi-gateway`、`eve-sentry-bot`

## 背景

服务端已经提供 `/api/v1/bootstrap`、`/api/v1/events`、只读服务密钥和私有 ESI
Gateway。机器人依赖 bootstrap、SSE 事件、`hostile_count` 和 `monitoring_nodes`；
客户端还需要稳定的上报请求体和错误码。实现细节分散在不同仓库，不能把应用模块
直接作为共享库引用。

## 决策

1. 本仓库是唯一的线协议登记处。JSON/OpenAPI、示例和 DTO 在这里发布；实现仓库
   不互相复制内部模块。
2. 所有时间使用带时区的 ISO-8601 字符串；未知数值使用 `null`，不以空字符串代替。
3. 所有顶层快照/事件携带 `schema_version`。消费者必须忽略未知字段，生产者只能在
   同一 schema 大版本内新增可选字段。
4. `map.systems[].hostile_count` 和事件中的 `hostile_count` 是按客户端去重后的权威
   人数。不得用 `names` 或 `active_intel` 数组长度替代它。
5. SSE `id` 是断线续传游标。消费者只有在完整处理一个事件后推进本地游标；重连优先
   发送 `Last-Event-ID`，其次使用 `since`。
6. `monitoring_node` 是节点变化的兼容事件；`hostile_movement` 是人员跨星系移动的
   明确事件。服务端在尚未发出新事件时，消费者仍必须通过 bootstrap 的完整状态校准。
7. `hostile_movement` 只表达已确认的“来源星系→目标星系”移动，不表示攻击或战术建议。
   缺少稳定身份时不得猜测移动；消费者可退化为两个 bootstrap 状态变化。

## 版本策略

- 仓库版本：SemVer。
- schema 版本：`<name>.v<major>`，例如 `intel_bootstrap.v1`、
  `hostile_movement_event.v1`。
- PATCH：文档、示例修正，不改变验证结果。
- MINOR：新增可选字段、事件或端点；旧消费者必须继续工作。
- MAJOR：删除/重命名字段、改变类型、认证范围、游标或事件 ID 语义。
- 旧 `/api/*` 路由只作为服务端迁移兼容，不在新 DTO 中扩散；新接入统一使用
  `/api/v1/*`。

## 非目标

本 ADR 不规定数据库模型、评分算法、QQ 消息格式、UI 组件或 ESI 的内部缓存实现。

