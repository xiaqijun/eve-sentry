# API 参考

默认地址为 `http://127.0.0.1:8765`。桌面客户端和工作台主要使用 `/api/v1`；旧
`/api/*` 兼容路由仍存在；来袭分析读取专用的 `/api/v1/alert-history`，新接入应优先使用
v1。

## 认证

桌面客户端和服务账号使用：

```http
Authorization: Bearer eve_xxx
```

网页登录使用会话 Cookie；所有 POST、PUT、DELETE 请求还需携带登录响应或
`GET /api/v1/auth/me` 返回的 `X-CSRF-Token`。

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
| `POST` | `/api/v1/client/identity-checks` | 幂等提交 Listener 角色并立即返回任务状态；身份校验在服务端异步执行 |
| `POST` | `/api/v1/client/identity-check` | 旧版同步身份校验，仅用于滚动升级兼容 |
| `GET/POST` | `/api/v1/admin/users` | 用户列表和创建用户 |
| `POST` | `/api/v1/admin/users/{id}/status` | 启用或禁用用户 |
| `POST` | `/api/v1/admin/users/{id}/reset-password` | 重置管理员密码 |
| `DELETE` | `/api/v1/admin/users/{id}` | 删除用户 |
| `POST` | `/api/v1/admin/users/{id}/service-keys` | 创建只读服务密钥 |
| `POST` | `/api/v1/admin/users/{id}/characters` | 添加用户角色白名单 |
| `DELETE` | `/api/v1/admin/users/{id}/characters/{character_id}` | 删除角色白名单 |
| `GET/POST` | `/api/v1/admin/corporations` | 列出或添加允许军团 |
| `DELETE` | `/api/v1/admin/corporations/{corporation_id}` | 删除允许军团 |
| `GET` | `/api/v1/admin/audit` | 审计日志 |

异步身份接口在任务排队、处理中或等待重试时返回 `202`，已验证时返回 `200` 并携带解析
后的角色 ID。客户端可用相同角色集合重复提交来读取状态，不需要保存任务 ID；服务端按
设备密钥和规范化角色集合保证幂等。

## 监控与事件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/ocr/snapshot` | 上传当前 OCR 名单和红框计数 |
| `POST` | `/api/v1/clients/heartbeats` | 上传客户端状态、窗口目标和最近异常 |
| `GET` | `/api/v1/clients` | 在线客户端和聚合状态 |
| `GET` | `/api/v1/active-intel` | 当前实时情报 |
| `GET` | `/api/v1/alert-history` | 按时间分页读取报表告警历史；生产 PostgreSQL 使用独立历史路径 |
| `GET` | `/api/v1/hostile-waves` | 按“出现至清空”记录的敌对星系波次 |
| `GET` | `/api/v1/bootstrap` | Web 和机器人使用的完整初始快照 |
| `GET` | `/api/v1/events` | SSE 实时事件流 |
| `GET` | `/api/v1/integrations/hostile-systems` | 第三方软件读取当前存在敌对的星系 |
| `GET` | `/api/v1/alerts` | 当前敌对告警 |
| `GET` | `/api/v1/alerts/{id}` | 单条告警详情 |
| `GET/POST` | `/api/v1/reports` | 历史上报读取和写入 |
| `GET/POST` | `/api/v1/observations` | 规范化观察记录读取和写入 |
| `POST` | `/api/v1/channel-lines` | 独立频道客户端上传日志行 |

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
部署会直接按 `received_at` 索引分页，并仅使用报告快照和本地缓存重建分类，避免历史
查询触发外部 ESI/zKill 请求或污染实时告警缓存。
`/api/v1/hostile-waves` 支持 `since` 和 `limit`。`since` 会返回结束时间晚于该时刻或仍在
进行中的波次；每个星系从敌对总数由 0 变为大于 0 时创建波次，回到 0 时写入
`cleared_at`，之后再次出现会创建新的 `id`。该接口使用独立 PostgreSQL 生命周期表，
不会恢复启动时的全量 active-intel 历史加载。

敌对告警的 `verified_characters` 始终保留 `character_id` 和 `name`。取得外部统计时会额外
包含可选的 `zkill` 对象：

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

`zkill` 缺失表示尚未抓取、角色无统计或外部服务暂不可用。消费者必须把字段视为可选，
不能使用告警 `score` 推断或回填 `danger_ratio`。zKillboard 数据只用于展示和研判，
不影响 `classification`、告警生成或确认状态。

SSE 常用查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `since` | 空 | ISO 时间或事件游标 |
| `limit` | `50` | 单次读取上限 |
| `timeout` | `30` | 服务端等待秒数 |
| `heartbeat` | `15` | SSE 心跳秒数 |
| `bootstrap` | `false` | 首次连接是否发送精简活跃状态快照 |

客户端必须保存并推进事件游标，不能在每次重连时反复拉取历史事件。
预警客户端不调用完整 `/api/v1/bootstrap`；它在 SSE 上请求精简快照，只包含活跃情报、
活跃告警和监控节点。生成该快照时只处理活跃情报引用的报告。

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
消失。接口不返回角色、客户端、评分或其他内部告警信息。

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
| `GET` | `/api/v1/esi/session` | 可选位置和 contacts 快照 |
| `GET/POST` | `/api/v1/esi/login` | 态势页 ESI 授权状态和启动 |

`/api/v1/kill-activity/*` 仅保留兼容行为；人员 zKillboard 统计通过告警的
`verified_characters[].zkill` 返回，不新增同步查询接口。
`/api/v1/map/neighborhood` 接受逗号分隔的 `systems`、`system_ids` 和 `hops` 参数，
`hops` 默认 `3` 且最大为 `5`。响应只包含任一中心星系指定跳数内的节点和节点间连线；
预警浮窗使用该接口，避免传输完整地图。

地图源支持 `builtin`、`manual` 和 `sde`。生产推荐使用官方 SDE，并通过
`scripts/sync_sde.py` 同步到服务端运行目录。
