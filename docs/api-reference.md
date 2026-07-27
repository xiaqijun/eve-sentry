# API 参考

默认地址为 `http://127.0.0.1:8765`。桌面客户端和工作台主要使用 `/api/v1`；旧
`/api/*` 兼容路由仍存在，来袭报表当前仍读取 `/api/alerts`，新接入应优先使用 v1。

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
| `GET` | `/api/health` | 服务、存储、地图、ESI 和事件流状态 |
| `POST` | `/api/v1/auth/login` | 管理员密码登录 |
| `GET` | `/api/v1/auth/esi/start` | 开始普通用户 EVE SSO 登录 |
| `GET` | `/api/v1/auth/esi/callback` | 统一 EVE SSO 回调 |

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
| `POST` | `/api/v1/client/identity-check` | 提交 EVE Listener 角色校验 |
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

## 监控与事件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/ocr/snapshot` | 上传当前 OCR 名单和红框计数 |
| `POST` | `/api/v1/clients/heartbeats` | 上传客户端状态 |
| `GET` | `/api/v1/clients` | 在线客户端和聚合状态 |
| `GET` | `/api/v1/active-intel` | 当前实时情报 |
| `GET` | `/api/v1/bootstrap` | Web、机器人和预警客户端初始快照 |
| `GET` | `/api/v1/events` | SSE 实时事件流 |
| `GET` | `/api/v1/alerts` | 当前敌对告警 |
| `GET` | `/api/v1/alerts/{id}` | 单条告警详情 |
| `POST` | `/api/v1/alerts/{id}/ack` | 确认告警 |
| `GET/POST` | `/api/v1/reports` | 历史上报读取和写入 |
| `GET/POST` | `/api/v1/observations` | 规范化观察记录读取和写入 |
| `POST` | `/api/v1/channel-lines` | 独立频道客户端上传日志行 |

SSE 常用查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `since` | 空 | ISO 时间或事件游标 |
| `limit` | `50` | 单次读取上限 |
| `timeout` | `30` | 服务端等待秒数 |
| `heartbeat` | `15` | SSE 心跳秒数 |
| `bootstrap` | `false` | 首次连接是否发送 Bootstrap |

客户端必须保存并推进事件游标，不能在每次重连时反复拉取历史事件。

## 地图、配置与 ESI

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/map` | 当前地图快照 |
| `GET` | `/api/v1/map/systems/{system_id}` | 星系节点详情 |
| `GET/PUT` | `/api/v1/config` | 敌我分类配置 |
| `GET` | `/api/v1/characters/{character_id}` | 角色资料 |
| `GET` | `/api/v1/characters/by-name/{name}` | 按准确名称解析角色 |
| `GET` | `/api/v1/systems/{system_id}` | 星系资料 |
| `GET` | `/api/v1/systems/by-name/{name}` | 按名称解析星系 |
| `GET` | `/api/v1/esi/status` | ESI 配置和授权状态 |
| `GET` | `/api/v1/esi/session` | 可选位置和 contacts 快照 |
| `GET/POST` | `/api/v1/esi/login` | 态势页 ESI 授权状态和启动 |

`/api/v1/kill-activity/*` 仅保留兼容行为；当前未启用 zKillboard 数据时返回 404。

地图源支持 `builtin`、`manual` 和 `sde`。生产推荐使用官方 SDE，并通过
`scripts/sync_sde.py` 同步到服务端运行目录。
