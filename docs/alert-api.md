# 预警消息 API 接入指南

本文面向需要从 EVE Sentry 获取实时预警的第三方程序。示例中的服务端地址统一写为
`https://YOUR_SERVER`，请替换为实际部署地址。

服务端事件字段和兼容路由的完整定义见[完整 API 参考](api-reference.md)。SSE 首字节、
快照复用和心跳约束见[SSE 性能与重连约束](sse-performance-guardrails.md)；机器人消费端
还需遵守[机器人 SSE 重连约束](../bot/docs/sse-reconnect-guardrails.md)。

## 接口选择

| 需求 | 推荐接口 | 返回方式 | 只读服务密钥 |
| --- | --- | --- | --- |
| 实时接收预警、清空和状态同步 | `GET /api/v1/events` | SSE 长连接 | 支持 |
| 只获取当前存在敌对的星系名称 | `GET /api/v1/integrations/hostile-systems` | JSON 轮询 | 支持 |
| 查询当前完整活动告警 | `GET /api/v1/alerts` | JSON 轮询 | 不支持 |

第三方集成优先使用只读服务密钥。只有确实需要完整活动告警列表时，才使用桌面设备密钥
或网页登录会话访问 `/api/v1/alerts`。

## 认证

管理员在系统管理中为目标账号创建只读服务密钥。完整密钥只在创建时显示一次，请通过
HTTP 请求头传递：

```http
Authorization: Bearer eve_xxx
```

只读服务密钥只能读取以下接口：

- `/api/v1/events`
- `/api/v1/bootstrap`
- `/api/v1/alert-history`
- `/api/v1/hostile-waves`
- `/api/v1/integrations/hostile-systems`
- `/api/v1/ocr/query` 和 `/api/v1/ocr/query/{query_id}`

不要把密钥放入 URL、日志或前端源码。公网调用必须使用 HTTPS。密钥被吊销、所属账号被
禁用或删除后，已有 SSE 连接也会被服务端主动断开。

其中 `POST /api/v1/ocr/query` 是只读服务密钥唯一允许的命令写入例外；它只创建有界的
一次性客户端查询，不直接修改持久化情报。

## 实时预警事件流

### 建立连接

```http
GET /api/v1/events?bootstrap=1&heartbeat=15 HTTP/1.1
Host: YOUR_SERVER
Accept: text/event-stream
Authorization: Bearer eve_xxx
```

使用 curl 调试：

```bash
curl -N --fail-with-body \
  -H "Accept: text/event-stream" \
  -H "Authorization: Bearer eve_xxx" \
  "https://YOUR_SERVER/api/v1/events?bootstrap=1&heartbeat=15"
```

响应类型为 `text/event-stream; charset=utf-8`。服务端默认在 30 秒后正常结束一次 SSE
响应，并默认每 15 秒发送注释心跳。正常 EOF 表示连接轮换，应立即重连；只有网络或协议
错误才使用有上限的指数退避。

### 查询参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `bootstrap` | `false` | 为 `true` 时发送当前活动状态快照，推荐始终启用 |
| `timeout` | `30` | 本次响应最长保持秒数，范围 `0` 至 `300`；`0` 表示读取当前一轮后结束 |
| `heartbeat` | `15` | 空闲时发送 SSE 注释心跳的间隔秒数，范围 `0` 至 `60`；`0` 表示关闭 |
| `limit` | `50` | 普通告警与持久状态事件的单页读取上限，最大 `1000`；为追平一致快照水位，一次响应可能连续读取多页状态事件 |
| `since` | 空 | ISO 8601 时间回退游标 |
| `min_score` | 空 | 只接收不低于该分数的告警，必须为非负整数 |
| `min_level` | 空 | 最低等级：`low`、`medium`、`high` 或 `critical` |

重连时应把最后处理成功的可靠 SSE 游标持久化并放入 `Last-Event-ID` 请求头。
`state:<sequence>` 直接恢复状态事件序列；一旦取得该游标，只能由更高状态序号替换，后续
alert/report、`presence_*` 或 `monitoring_node` ID 不得把它降级覆盖。已存储的 alert/report ID
可解析到报告流游标，ISO 8601 ID 可按时间恢复；没有状态序号时，合成 ID 由 `bootstrap`
权威对账，并可通过 `since` 提供时间回退。任何显式 `state:*`（包括 `state:0`）都优先于并
禁用 `since` 过滤。机器人保存的状态 ACK 不使用短期去重 TTL。

### `bootstrap` 事件

`bootstrap=1` 时，本次连接会收到当前活动状态。PostgreSQL 实现用同一条查询取得活动条目、
其引用报告和已提交状态事件水位 `W`；若游标落后，服务端先按序重放所有 `seq <= W` 的
`alert`/`safe`，再发送 ID 为 `state:W` 的 Bootstrap。并发产生的 `seq > W` 只在下一轮发送，
因此 Bootstrap 不会回到刚发送的进入/清空事件之前。活动情报、告警或监控节点位置、健康、
生命周期变化时，同一连接还可能再次收到新快照；消费者仍应把重复清空按幂等操作处理。
告警重建只读取同一数据库视图中的报告和持久化身份元数据，不混用进程内或磁盘实时缓存；
OCR/ESI 丰富、active 更新和相应状态事件以同一事务发布。

PostgreSQL 流在派生的报告或 Presence 告警之后可能发送只有 `id: state:W`、没有 `event`/
`data` 的 SSE 控制块。它只更新原生 EventSource 的自动重连 ID，不派发业务事件；手写 SSE
解析器必须接受并忽略无 `data` 块。

```text
id: state:124
event: bootstrap
data: {"schema_version":"intel_bootstrap.v1","generated_at":"2026-08-04T12:00:00+00:00","map":{"systems":[{"name":"S-KSWL","system_name":"S-KSWL","hostile_count":2}],"summary":{"system_count":1,"alert_count":1}},"alerts":[{"id":"evt_0123456789abcdef","system_name":"S-KSWL","hostile_count":2}],"active_intel":[{"system_name":"S-KSWL"}],"clients":{"heartbeats":[]}}

```

建议使用 `map.systems` 初始化或重新校准当前预警状态。红色图标数量是当前预警的权威状态，
OCR 人员名单只是额外信息；因此即使尚未生成 OCR 告警记录，`map.systems` 也可能已经包含
即时的红色星系：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | string | 当前为 `intel_bootstrap.v1` |
| `generated_at` | string | 快照生成时间，ISO 8601 |
| `map.systems[].name` | string | 存在敌对的星系名称 |
| `map.systems[].hostile_count` | integer | 该星系当前聚合敌对人数 |
| `map.summary.system_count` | integer | 当前预警星系数 |
| `map.summary.alert_count` | integer | 当前活动告警记录数 |
| `alerts` | array | 当前活动告警详情 |
| `active_intel` | array | 当前活动情报 |
| `clients` | object | 在线客户端和监控位置快照 |
| `monitoring_node_changes` | array | 本次快照相对上次快照的生命周期、位置和健康状态变化 |
| `monitoring_nodes` | array | 当前仍可见的非 `removed` 节点；可处于 `online`、`degraded` 或 `offline` |
| `monitoring_nodes_version` | string | 由节点 ID、星系和健康状态生成；敌对人数不参与版本计算 |

`hostile_count` 已由服务端按监控客户端去重和聚合。调用方不应自行累加 `alerts` 或
`active_intel` 来替代该值；`alerts` 可能要等 OCR 增效信息到达后才出现。

### 监控节点变化

当连接使用 `bootstrap=1` 时，服务端会在监控节点状态发生变化后更新快照中的
`monitoring_node_changes`。首次连接返回空数组；后续只包含本次变化。与此同时，
`monitoring_nodes` 始终提供完整可见节点列表，机器人应在节点变化时优先推送该列表，
并可使用 `monitoring_nodes_version` 去重和在断线重连后校正漏报。

```json
{
  "monitoring_node_changes": [
    {
      "change": "online",
      "node_id": "client:detector-client:test:pilot-alpha",
      "character_name": "Pilot Alpha",
      "source_instance": "EVE - Pilot Alpha",
      "system_name": "Jita",
      "system_id": 30000142,
      "health_status": "online"
    },
    {
      "change": "moved",
      "node_id": "client:detector-client:test:pilot-beta",
      "character_name": "Pilot Beta",
      "from_system": "Jita",
      "to_system": "Tama",
      "system_name": "Tama",
      "system_id": 30002813,
      "health_status": "online"
    },
    {
      "change": "offline",
      "node_id": "client:detector-client:test:pilot-gamma",
      "character_name": "Pilot Gamma",
      "system_name": "Amarr",
      "health_status": "offline",
      "previous_health_status": "degraded"
    }
  ],
  "monitoring_nodes_version": "8d6a0d2a6c6bb1a0",
  "monitoring_nodes": [
    {
      "client_id": "detector-client:test:pilot-alpha",
      "system_name": "Jita",
      "health_status": "online"
    },
    {
      "client_id": "detector-client:test:pilot-beta",
      "system_name": "Tama",
      "health_status": "online"
    },
    {
      "client_id": "detector-client:test:pilot-gamma",
      "system_name": "Amarr",
      "health_status": "offline"
    }
  ]
}
```

`change` 可取 `online`、`degraded`、`offline`、`removed` 或 `moved`。健康状态变化会带
`previous_health_status`；同一次更新若同时发生位置和健康变化，可以产生两条 change。
`monitoring_nodes` 不是“所有在线节点”，而是所有尚未 `removed` 且具有有效星系的可见
节点。机器人应把变化信息作为提示，并以该完整列表作为最终状态，使用
`monitoring_nodes_version` 去重。按当前 10 秒心跳配置计算，服务端为调度和网络抖动预留
5 秒宽限，节点大约在 15 秒进入 `degraded`、25 秒进入 `offline`、35 秒进入 `removed`；
星系变化只在同一节点的
`system_name` 实际改变时产生。`capture_online=false` 会在父 heartbeat 仍在线时直接把目标
标为 `offline`，不要求先经过 `degraded`。

### `monitoring_node` 事件

为方便机器人直接推送消息，节点状态变化还会作为独立 SSE 事件发送。该事件与同一轮
`bootstrap` 使用相同的 SSE `id`，不会影响告警断线续传。

```text
id: 2026-08-10T01:00:00+00:00
event: monitoring_node
data: {"schema_version":"monitoring_node_event.v1","generated_at":"2026-08-10T01:00:00+00:00","changes":[{"change":"moved","node_id":"client:detector-client:test:pilot-alpha","character_name":"Pilot Alpha","from_system":"Jita","to_system":"Tama","system_name":"Tama","health_status":"online"}],"nodes_version":"8d6a0d2a6c6bb1a0","nodes":[{"client_id":"detector-client:test:pilot-alpha","system_name":"Tama","health_status":"online"}]}
```

机器人可以直接监听 `monitoring_node`，在收到事件时推送 `nodes` 中的完整可见节点列表；
`bootstrap.monitoring_node_changes` 仍会保留，用于不支持新事件名的兼容消费者。

### `alert` 事件

wire `alert` 当前有两种载荷，消费者必须按字段能力处理，不能假设所有字段都存在：

- 持久状态事件：`id` 为 `state:<sequence>`，`data.event_type` 为 `alert.entered` 或
  `alert.updated`，重点字段是 `event_key`、`system_name`、`hostile_count`、
  `hostile_personnel` 和 `active`；
- 告警报告事件：`id` 通常为 `evt_*`，可以包含 `names`、`character_ids`、`level`、`score`、
  `verified_characters` 和 `evidence` 等报告字段。

持久状态事件示例：

```text
id: state:124
event: alert
data: {"id":"state:124","event_key":"alert.updated:s-kswl:...","event_type":"alert.updated","system_name":"S-KSWL","hostile_count":2,"hostile_personnel":[],"active":true,"created_at":"2026-09-08T12:00:00+00:00","presence_only":true}
```

告警报告示例：

```text
id: evt_0123456789abcdef
event: alert
data: {"id":"evt_0123456789abcdef","level":"critical","score":100,"system_name":"S-KSWL","system":"S-KSWL","system_id":30002813,"names":["Example Pilot"],"character_ids":[443630591],"classification":"red","hostile_count":1,"created_at":"2026-08-04T12:00:00+00:00","seen_at":"2026-08-04T12:00:00+00:00","source_observation_id":"obs_0123456789abcdef","verified_characters":[{"character_id":443630591,"name":"Example Pilot"}]}

```

红色图标的 detector Presence 状态也会作为 `alert` 发送。它不要求先生成历史人员报告；
没有已确认人员时 `presence_only=true`，`hostile_personnel` 为空，但 `hostile_count` 仍是
有效的权威人数。数量变化可以生成新的 `alert.updated`，清空后再次进入会生成新的
`alert.entered`；消费者必须按事件 ID 或 `event_key` 去重，不能忽略数量更新。

调用方常用字段如下。“必需”按对应载荷类型计算：

| 字段 | 类型 | 必需范围 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 两者 | 事件/告警 ID，也用于断线续传和去重 |
| `system_name` | string | 两者 | 敌对所在星系；`system` 是兼容别名 |
| `created_at` | string | 两者 | 服务端生成时间，ISO 8601 |
| `event_type` | string | 状态事件 | `alert.entered` 或 `alert.updated` |
| `event_key` | string | 状态事件 | 状态事件幂等键 |
| `active` | boolean | 状态事件 | `alert` 状态事件为 `true` |
| `hostile_count` | integer | 状态事件 | 当前权威敌对人数；报告载荷中可选 |
| `hostile_personnel` | object[] | 状态事件 | 当前已确认敌对人员，可能为空 |
| `system_id` | integer/null | 可选 | EVE 星系 ID，无法解析时为 `null` |
| `names` | string[] | 报告事件 | 本条报告识别或上报的角色名称 |
| `character_ids` | integer[] | 报告事件 | 本条报告已解析的角色 ID，可能为空 |
| `active_names` | string[] | 报告事件可选 | 当前活动快照中的完整已确认名单 |
| `active_character_ids` | integer[] | 报告事件可选 | 当前活动快照中已解析的完整角色 ID |
| `classification` | string | 报告事件可选 | 当前敌我分类，敌对通常为 `red` |
| `level` / `score` | string / integer | 报告事件 | 兼容告警等级和分数 |
| `source_observation_id` | string | 报告事件可选 | 来源观察记录 ID |
| `verified_characters` / `evidence` | object[] | 报告事件可选 | ESI 角色详情和判定依据 |

`verified_characters[].zkill` 是可选的外部统计。消费者必须允许它缺失，并忽略未来新增的
未知字段。服务端在敌对历史和活动告警中排除 `classification=white` 以及带有
`friendly_*` 证据的记录；detector 人员只有在 ESI 身份解析完成且当前分类为 `red` 时才会
进入人员明细。机器人或其他集成应使用 `hostile_count` 作为人数；状态事件使用
`hostile_personnel`，报告事件可使用 `active_names` 读取当前已确认名单。`names` 只表示一条
报告，不能作为完整名单；Presence-only 状态没有人员时名单可以为空，但人数仍然有效。

QQ 机器人在人员表的 zKill 列直接输出完整的 `https://zkillboard.com/character/{id}/`
地址。不要依赖仅包含图标的 Markdown 链接：部分 QQ 群 Markdown 渲染器会隐藏其链接目标，
导致消息中只剩 `🔗` 且无法点击。

### 内部状态事件与 SSE wire 事件映射

PostgreSQL 事件日志启用后，服务端发送带稳定序号游标的状态事件，但 wire `event` 仍保持
固定兼容名称：

| 内部 `data.event_type` | SSE wire `event` | 含义 |
| --- | --- | --- |
| `alert.entered` | `alert` | 威胁进入活动状态 |
| `alert.updated` | `alert` | 活动威胁更新 |
| `alert.cleared` | `safe` | 威胁解除 |

```text
id: state:123
event: safe
data: {"id":"state:123","event_key":"alert.cleared:s-kswl:...","event_type":"alert.cleared","system_name":"S-KSWL","hostile_count":0,"active":false,"created_at":"2026-09-07T12:05:00+00:00","message":"✅ S-KSWL 清空"}
```

`alert.entered` 表示从无敌对到有敌对，`alert.updated` 表示权威人数或已确认名单变化，
`alert.cleared` 表示服务端状态从非空转为空。`state:<seq>` 是 PostgreSQL 事件序号，
客户端应保存它并在 `Last-Event-ID` 中续传。消费者按 wire `event` 分派消息，并可用
`data.event_type` 区分内部转换；事件已经在状态事务中落库，不由每个 SSE 连接临时推导。
`bootstrap` 和 `monitoring_node` 是另外两类 wire 事件，节点变化后应以伴随的
`bootstrap` 为最终状态。

### `safe` 事件

一个星系的最后一条活动敌对证据清空时发送：

```text
id: state:125
event: safe
data: {"id":"state:125","event_key":"alert.cleared:s-kswl:...","event_type":"alert.cleared","system_name":"S-KSWL","system":"S-KSWL","hostile_count":0,"active":false,"created_at":"2026-08-04T12:05:00+00:00","message":"✅ S-KSWL 清空"}

```

收到后应清除该星系的本地预警状态；本地已经不存在该星系时只确认游标，不重复产生清空
通知。若连接期间漏掉 `safe`，下一次 `bootstrap` 快照仍可用来校准完整活动状态。时间游标
必须单调前进，迟到事件不得覆盖更晚的 Bootstrap 游标。

### 心跳

没有业务事件时，服务端可能发送 SSE 注释：

```text
: keepalive

```

这是连接保活信息，不是预警消息，调用方直接忽略即可。

### Python 示例

以下示例只依赖 Python 标准库，保存最后事件 ID，并在服务端正常关闭连接后重连：

```python
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVER = "https://YOUR_SERVER"
API_KEY = "eve_xxx"
last_event_id = ""


def state_sequence(value):
    if not value.startswith("state:"):
        return None
    try:
        return max(0, int(value.split(":", 1)[1]))
    except ValueError:
        return None

while True:
    headers = {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {API_KEY}",
    }
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id

    request = Request(
        f"{SERVER}/api/v1/events?bootstrap=1&heartbeat=15",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=45) as response:
            event_name = "message"
            event_id = ""
            data_lines = []
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        if event_name in {"bootstrap", "monitoring_node", "alert", "safe"}:
                            print(event_name, payload)
                        current_seq = state_sequence(last_event_id)
                        event_seq = state_sequence(event_id)
                        if event_id and (
                            current_seq is None
                            or (event_seq is not None and event_seq >= current_seq)
                        ):
                            last_event_id = event_id
                    event_name, event_id, data_lines = "message", "", []
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("id:"):
                    event_id = line[3:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("预警 API 认证失败，请检查或轮换服务密钥") from exc
        time.sleep(3)
    except URLError:
        time.sleep(3)
```

生产程序应把 `last_event_id` 持久化到本地，并对短时网络错误使用有上限的指数退避。没有
任何游标且请求 `bootstrap=1` 表示只取当前状态；只有显式发送 `Last-Event-ID: state:0`
才要求从保留日志起点完整重放。

## 当前敌对星系轮询

只关心“哪些星系当前存在敌对”时，使用最小化接口：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer eve_xxx" \
  "https://YOUR_SERVER/api/v1/integrations/hostile-systems"
```

```json
{
  "schema_version": "hostile_systems.v1",
  "generated_at": "2026-08-04T12:00:00+00:00",
  "count": 2,
  "systems": ["S-KSWL", "Tama"]
}
```

`systems` 按名称排序，只包含当前仍有敌对证据的星系。星系清空后会从下一次响应中消失。
接口不返回人员、客户端、评分或其他内部信息。建议轮询间隔不低于 5 秒。

## 当前完整活动告警

需要人员、证据和评分等完整字段时，可以使用：

```http
GET /api/v1/alerts?limit=100&min_level=medium
Authorization: Bearer eve_桌面设备密钥
```

```json
{
  "alerts": [
    {
      "id": "evt_0123456789abcdef",
      "system_name": "S-KSWL",
      "names": ["Example Pilot"],
      "hostile_count": 1,
      "level": "critical",
      "score": 100,
      "created_at": "2026-08-04T12:00:00+00:00"
    }
  ],
  "count": 1
}
```

支持 `since`、`limit`、`min_score` 和 `min_level`，默认最多返回 100 条，`limit` 最大为
1000。该接口只返回当前仍然活动的敌对告警，不是历史记录。只读服务密钥访问它会得到
`403 service_key_scope_denied`；第三方服务需要完整告警时，优先改用 SSE。

## 状态码与排查

| 状态码 | 常见含义 | 处理方式 |
| --- | --- | --- |
| `200` | JSON 请求成功，或 SSE 连接建立 | 正常处理 |
| `400` | 查询参数无效 | 检查 `limit`、`timeout`、`heartbeat` 和等级值 |
| `401` | 缺少认证、密钥无效或已吊销 | 停止重试并更换密钥 |
| `403` | 密钥类型没有该接口权限，或账号被禁用 | 使用允许的接口或联系管理员 |
| `404` | 路径不存在 | 确认使用 `/api/v1` 路径 |

所有 HTTP 响应都包含 `X-Request-ID`。排查问题时记录该值和请求时间，但不要记录
`Authorization` 请求头。
