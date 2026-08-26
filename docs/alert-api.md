# 预警消息 API 接入指南

本文面向需要从 EVE Sentry 获取实时预警的第三方程序。示例中的服务端地址统一写为
`https://YOUR_SERVER`，请替换为实际部署地址。

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
- `/api/v1/integrations/hostile-systems`

不要把密钥放入 URL、日志或前端源码。公网调用必须使用 HTTPS。密钥被吊销、所属账号被
禁用或删除后，已有 SSE 连接也会被服务端主动断开。

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

响应类型为 `text/event-stream; charset=utf-8`。默认保持长期连接；只有显式设置非零
`timeout` 时，服务端才会在到期后正常关闭。正常 EOF 应立即重连，连接异常才使用退避。

### 查询参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `bootstrap` | `false` | 为 `true` 时发送当前活动状态快照，推荐始终启用 |
| `timeout` | 空 | 本次连接最长保持秒数，范围 `0` 至 `300`；省略表示长期连接，`0` 表示读取当前一轮后结束 |
| `heartbeat` | `15` | 空闲时发送 SSE 注释心跳的间隔秒数，范围 `0` 至 `60`；`0` 表示关闭 |
| `limit` | `50` | 每轮最多读取的活动告警数，最大 `1000` |
| `since` | 空 | ISO 8601 时间或上次事件游标 |
| `min_score` | 空 | 只接收不低于该分数的告警，必须为非负整数 |
| `min_level` | 空 | 最低等级：`low`、`medium`、`high` 或 `critical` |

重连时优先把最后处理成功的 SSE `id` 放入 `Last-Event-ID` 请求头。服务端会从该事件之后
继续发送；未保存事件 ID 时，可以用 `since` 传递最后处理时间。`Last-Event-ID` 的优先级
高于 `since`。

### `bootstrap` 事件

`bootstrap=1` 时，连接建立后会收到当前活动状态。活动情报、告警或在线监控位置变化时，
同一连接还可能再次收到新的快照。

```text
id: evt_0123456789abcdef
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
| `monitoring_node_changes` | array | 本次快照相对上次快照的上线、下线或换星系变化 |
| `monitoring_nodes` | array | 当前全部在线监控节点快照；节点变化时用于校正漏报 |
| `monitoring_nodes_version` | string | 当前在线节点快照版本；节点列表未变化时保持不变 |

`hostile_count` 已由服务端按监控客户端去重和聚合。调用方不应自行累加 `alerts` 或
`active_intel` 来替代该值；`alerts` 可能要等 OCR 增效信息到达后才出现。

### 监控节点变化

当连接使用 `bootstrap=1` 时，服务端会在监控节点状态发生变化后更新快照中的
`monitoring_node_changes`。首次连接返回空数组；后续只包含本次变化。与此同时，
`monitoring_nodes` 始终提供完整在线节点列表，机器人应在节点变化时优先推送该列表，
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
      "system_id": 30000142
    },
    {
      "change": "moved",
      "node_id": "client:detector-client:test:pilot-beta",
      "character_name": "Pilot Beta",
      "from_system": "Jita",
      "to_system": "Tama",
      "system_name": "Tama",
      "system_id": 30002813
    },
    {
      "change": "offline",
      "node_id": "client:detector-client:test:pilot-gamma",
      "character_name": "Pilot Gamma",
      "system_name": "Amarr"
    }
  ],
  "monitoring_nodes_version": "8d6a0d2a6c6bb1a0",
  "monitoring_nodes": [
    {
      "client_id": "detector-client:test:pilot-alpha",
      "system_name": "Jita"
    },
    {
      "client_id": "detector-client:test:pilot-beta",
      "system_name": "Tama"
    }
  ]
}
```

`change` 取值为 `online`、`offline` 或 `moved`。机器人应把变化信息作为提示，并以
`monitoring_nodes` 的完整列表作为最终状态，使用 `monitoring_nodes_version` 去重。
下线既包括客户端主动停止监控，也包括心跳超时；星系变化只在同一节点的
`system_name` 实际改变时产生。

### `monitoring_node` 事件

为方便机器人直接推送消息，节点状态变化还会作为独立 SSE 事件发送。该事件与同一轮
`bootstrap` 使用相同的 SSE `id`，不会影响告警断线续传。

```text
id: 2026-08-10T01:00:00+00:00
event: monitoring_node
data: {"schema_version":"monitoring_node_event.v1","generated_at":"2026-08-10T01:00:00+00:00","changes":[{"change":"moved","node_id":"client:detector-client:test:pilot-alpha","character_name":"Pilot Alpha","from_system":"Jita","to_system":"Tama","system_name":"Tama"}],"nodes_version":"8d6a0d2a6c6bb1a0","nodes":[{"client_id":"detector-client:test:pilot-alpha","system_name":"Tama"}]}
```

机器人可以直接监听 `monitoring_node`，在收到事件时推送 `nodes` 中的完整在线节点列表；
`bootstrap.monitoring_node_changes` 仍会保留，用于不支持新事件名的兼容消费者。

### `alert` 事件

发现新的活动敌对证据时发送：

```text
id: evt_0123456789abcdef
event: alert
data: {"id":"evt_0123456789abcdef","level":"critical","score":100,"system_name":"S-KSWL","system":"S-KSWL","system_id":30002813,"names":["Example Pilot"],"character_ids":[443630591],"classification":"red","hostile_count":1,"created_at":"2026-08-04T12:00:00+00:00","seen_at":"2026-08-04T12:00:00+00:00","source_observation_id":"obs_0123456789abcdef","verified_characters":[{"character_id":443630591,"name":"Example Pilot"}]}

```

调用方通常只需要以下字段：

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 告警唯一 ID，也用于断线续传和去重 |
| `system_name` | string | 是 | 敌对所在星系；`system` 是兼容别名 |
| `system_id` | integer/null | 是 | EVE 星系 ID，无法解析时为 `null` |
| `names` | string[] | 是 | 本次识别或上报的角色名称 |
| `character_ids` | integer[] | 是 | 已解析的角色 ID，可能为空 |
| `hostile_count` | integer | 否 | 监控客户端确认的当前敌对人数 |
| `active_names` | string[] | 否 | detector 客户端在该星系当前活动快照中的完整角色名单；不要用单条 `names` 的长度计算人数 |
| `active_character_ids` | integer[] | 否 | 当前活动快照中已解析的完整角色 ID 列表 |
| `classification` | string | 否 | 当前敌我分类，敌对通常为 `red` |
| `level` | string | 是 | 兼容告警等级 |
| `score` | integer | 是 | 兼容告警分数，不等同于 zKill 威胁度 |
| `created_at` | string | 是 | 服务端生成时间，ISO 8601 |
| `source_observation_id` | string | 是 | 来源观察记录 ID |
| `verified_characters` | object[] | 是 | 经 ESI 确认的角色；可能为空 |
| `evidence` | object[] | 是 | 告警判定依据 |

`verified_characters[].zkill` 是可选的外部统计。消费者必须允许它缺失，并忽略未来新增的
未知字段。机器人或其他集成应使用 `hostile_count` 作为人数，使用 `active_names` 作为
detector 当前名单；`names` 只表示这一条告警记录，不能作为完整名单。presence-only
告警没有角色名单时，`active_names` 可能为空，但 `hostile_count` 仍然有效。

### `safe` 事件

一个星系的最后一条活动敌对证据清空时发送：

```text
id: 2026-08-04T12:05:00+00:00
event: safe
data: {"system_name":"S-KSWL","system":"S-KSWL","hostile_count":0,"active":false,"created_at":"2026-08-04T12:05:00+00:00","message":"✅ S-KSWL 清空"}

```

收到后应清除该星系的本地预警状态。若连接期间漏掉 `safe`，下一次 `bootstrap` 快照仍可
用来校准完整活动状态。

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
                        if event_id:
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

生产程序应把 `last_event_id` 持久化到本地，并对短时网络错误使用有上限的指数退避。

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
