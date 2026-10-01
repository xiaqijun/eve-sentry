# 认证与账号管理

EVE Sentry 使用本地账号会话和 API 密钥保护管理端、客户端和服务间接口。EVE
Online OAuth2、Listener 身份扫描、角色/军团白名单以及自动密钥风控均已移除，
不会再因为 EVE 角色、军团或联盟信息拒绝账号或密钥。

## 认证方式

| 方式 | 用途 | 生命周期 |
| --- | --- | --- |
| 管理端账号会话 | Web 控制台登录 | 服务端签发，默认 12 小时过期 |
| Seat 客户端密钥 | Windows 监控/预警客户端调用客户端接口 | 仅由 GloryNavy_Seat 签发、绑定和吊销 |
| 历史 desktop/service key | 迁移和管理兼容 | 不得访问监控/预警客户端接口 |

启用 `--auth-mode setup` 或 `enforce` 后，客户端使用：

```http
Authorization: Bearer eve_...
```

有效密钥只受密钥状态、所属用户状态和接口权限约束。新建密钥直接处于可用
状态，不需要 ESI、游戏客户端、Chatlogs 或额外的身份上报。

## 账号管理

管理员可以在 Web 控制台中创建用户、重置密码、启用/禁用用户，并查看密钥状态、
客户端心跳和审计日志。客户端密钥统一由 GloryNavy_Seat 签发和管理，Sentry 不再
提供 desktop/service_readonly 创建、恢复或删除入口；Seat 密钥的生命周期操作必须
回到 GloryNavy_Seat 完成。禁用本地用户会同时使其 Seat principal 失效。

旧数据库中可能仍保留 `auth_settings`、`auth_identity_jobs`、角色白名单和
允许军团表，用于平滑升级和历史审计。服务端不再读取这些表来做授权，也不会
创建新身份任务；后续数据库维护窗口可以再清理这些遗留表。

## 客户端行为

客户端启动监控或预警时，只检查服务端地址和本地 API 密钥配置，然后直接开始
业务连接。新客户端不会扫描 EVE Chatlogs，也不会调用身份校验接口。为兼容尚未升级的
旧客户端，服务端仍接受 `/api/v1/client/identity-check` 和
`/api/v1/client/identity-checks`，但只返回已确认的空结果，不读取 EVE 身份、不调用 ESI、
不创建身份任务，也不会因 Listener 缺失而延迟上线。

## 安全边界

移除 EVE 身份风控不等于移除基础认证：生产环境仍应使用 HTTPS/内网、最小权限
服务密钥、定期人工轮换密钥，并通过 Web 审计日志检查异常登录、客户端心跳和
密钥吊销记录。

## SeAT 业务认证

SeAT 密钥管理使用独立的服务端 Bearer Token；业务密钥认证由
`EVE_SENTRY_SERVER_SEAT_AUTH_MODE`（启动参数 `--seat-auth-mode`）单独控制，默认 `off`：

| 模式 | 行为 |
| --- | --- |
| `off` | Seat 集成未启用；不把任何旧客户端 key 当作 Seat key 或客户端绕过入口 |
| `enforce` | 仅有效的 SeAT 密钥、有效绑定和启用用户可以建立 SeAT principal |

SeAT `account_id` 不等同于本地 `auth_users.user_id`。`auth_external_accounts` 维护
provider=`seat` 下的一对一显式绑定（账号 ID、本地用户 ID、active/disabled、revision）。
冲突绑定返回 `409 seat_account_conflict`；吊销、绑定禁用或本地用户禁用会使已有 SeAT
SSE/请求失效。普通 API key 不读取这张绑定表。

SeAT principal 只能访问最小白名单：`monitor` 用于 bootstrap/map、心跳、Presence、OCR
和监控上报；`alert` 用于 bootstrap/map、active-intel、alert-history、hostile-waves、
hostile-systems 和 events。旧客户端的 `GET /api/v1/auth/me` 只用于只读验钥，返回已绑定
principal；其他 `/auth/*`、`/me/*`、`/admin/*` 统一返回 `403 seat_permission_denied`，不包含
结算或管理员能力。

同机 QQ 机器人使用现有 `/api/v1/bootstrap`、`/api/v1/events` 和按需 OCR 路径的
loopback 例外，必须带 `X-EVE-SENTRY-Embedded-Bot: 1`，不携带 Bearer 密钥；服务端只接受
来源为 `127.0.0.1` 或 `::1` 的请求。未带标记头的普通本机管理员/服务密钥请求仍按原策略
鉴权。该例外不适用于公网、内网其他主机或星图公开访问，不会改变 Seat 客户端认证规则。

收费预警 ACK 使用同一 `alert` 权限，但只绑定服务端已经记录的
`delivery_id + charge_event_id + revision + connection_id`。客户端必须在本地去重并完成 UI
投递后再发送 ACK；普通人工告警确认、bootstrap、重放、清空和旧客户端请求不会形成收费凭据。
`--allow-alert-consumption` 默认关闭，生产开启前需与 Seat exchange 的有限额度预留和退款
契约完成联调。
