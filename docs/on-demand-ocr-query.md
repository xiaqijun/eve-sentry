# 按需 OCR 查询客户端对接说明

本文只记录客户端需要实现的对接内容，不包含本次代码实现。服务端和 QQ 机器人
已经支持以下流程：

```text
QQ 机器人创建查询
  → 服务端在 detector heartbeat 响应中下发命令
  → 客户端对指定监控窗口执行一次 OCR
  → 客户端上传带 query_id 的 OCR 快照
  → 服务端聚合并由机器人轮询返回
```

服务端接口的完整说明见
[`eve-sentry/docs/api-reference.md`](https://github.com/xiaqijun/eve-sentry/blob/main/docs/api-reference.md)。

## 客户端需要实现的内容

### 1. 读取 heartbeat 响应中的命令

客户端现有心跳请求保持不变：

```http
POST /api/v1/clients/heartbeats
```

服务端响应除了原有 `heartbeat` 外，可能包含 `commands` 数组：

```json
{
  "ok": true,
  "heartbeat": {},
  "commands": [
    {
      "command": "ocr_query",
      "query_id": "ocrq_abc123",
      "target_client_id": "detector-client:device:window-1",
      "filters": {
        "name": "Alice"
      },
      "expires_at": "2026-09-04T03:00:00+00:00"
    }
  ]
}
```

客户端需要：

- 兼容没有 `commands` 字段的旧服务端；
- 只处理 `command == "ocr_query"` 的命令；
- 按 `target_client_id` 找到对应的本地监控窗口；
- `target_client_id` 为空时，按现有兼容策略处理默认监控窗口；
- `expires_at` 已过期的命令直接丢弃；
- 同一个 `query_id` 去重，避免重复执行。

一个 detector heartbeat 可能包含多个 `details.targets`，因此不能把命令只绑定到
heartbeat 的父 `client_id`；必须使用 target 的客户端 ID 区分不同 EVE 窗口。

### 2. 触发一次独立 OCR

收到有效命令后，对指定窗口当前保存的成员列表区域执行一次完整 OCR：

- 不改变常规 OCR 开关；
- 即使常规 OCR 被关闭，也必须执行这一次查询 OCR；
- 即使当前没有红色敌对图标，也必须执行这一次查询 OCR；
- 使用现有 OCR 引擎和现有名单清洗规则；
- 上传完整原始名单，不在客户端按敌我、军团或联盟过滤；
- 不需要新增持续轮询或新的定时器。

查询 OCR 与正常 OCR 可以复用同一帧和 OCR 调度器，但必须保证本次命令最终产生
一次独立上传。OCR 失败时保留 `query_id`，可在命令有效期内重试一次；不能静默标记
为成功。

### 3. 上传带 query_id 的 OCR 快照

使用现有接口：

```http
POST /api/v1/ocr/snapshot
```

在现有 payload 基础上增加 `query_id`：

```json
{
  "client_id": "detector-client:device:window-1",
  "source_instance": "EVE - Pilot",
  "system_name": "S-KSWL",
  "system_id": 30000123,
  "names": ["Alice", "Bob"],
  "hostile_icon_count": 1,
  "query_id": "ocrq_abc123"
}
```

注意事项：

- `client_id` 必须是命令中的 `target_client_id`，不能上传 heartbeat 父 ID；
- `query_id` 原样回传，不能为空或改写；
- `names` 可以为空数组，表示本次 OCR 没有识别到文本；
- 保留现有 `system_name`、`system_id`、`source_instance` 和时间字段；
- 查询上传不能覆盖或取消正常 OCR 快照队列；
- 建议查询上传使用独立队列键，例如 `query:{query_id}:{client_id}`。

服务端收到快照后会完成 ESI 识别和当前 active 名单聚合，客户端不需要实现额外的
角色、军团或联盟查询逻辑。

## 推荐的本地状态机

```text
收到命令
  ├─ 已过期/已完成 → 丢弃
  └─ 有效
       ↓
排入 one-shot OCR 队列
       ↓
捕获指定窗口并执行 OCR
       ├─ 成功 → 上传 query_id 快照 → 标记完成
       └─ 失败 → 保留 query_id，按有效期重试
```

建议状态只保留在内存中，不需要持久化查询任务。服务端查询任务是短生命周期任务，
服务端重启后正在进行的查询可能失效；普通 OCR 和预警上报不受影响。

## 兼容性与验收

实现必须满足：

1. 旧服务端不返回 `commands` 时，客户端现有监控和心跳行为不变；
2. 一个父 heartbeat 下两个监控窗口可以分别收到并执行查询；
3. 常规 OCR 关闭时，按需查询仍能上传一次 OCR；
4. 当前没有敌对图标时，按需查询仍能上传一次 OCR；
5. 同一 `query_id` 的重复 heartbeat 不会导致重复上传；
6. 正常 OCR、敌对数量上报和查询 OCR 三类队列互不覆盖；
7. 上传失败时沿用现有可靠上传和退避机制，并在查询有效期内重试。

客户端完成实现后，应在客户端仓库增加 HTTP contract tests，至少覆盖 heartbeat
命令解析、target 路由、query_id 透传、空 OCR 名单和重复命令去重。
