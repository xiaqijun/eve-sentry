# 认证与 EVE 身份校验

## 登录与密钥

| 身份 | 登录方式 | 权限 |
| --- | --- | --- |
| 管理员 | 用户名和密码 | 全部管理页面、账号安全和业务接口 |
| 普通用户 | EVE SSO | 态势页、报表和自己的设备密钥 |
| 桌面设备密钥（可选） | 填写后使用 `Authorization: Bearer <key>` | 有效密钥可访问客户端 API；开启密钥风控时同时启用 Listener 持续校验 |
| 只读服务密钥 | `Authorization: Bearer <key>` | 仅 Bootstrap、SSE 和第三方敌对星系接口 |

普通用户只有在 EVE SSO 角色属于管理员配置的允许军团时才能登录。管理员账号不使用
EVE SSO。EVE SSO 登录和态势页 ESI 授权共用一个应用和回调：

```text
/api/v1/auth/esi/callback
```

密码使用 Argon2id。会话和 API 密钥只保存哈希，完整密钥只在创建时显示一次。设备
密钥可吊销、重新启用或删除记录；已被用户禁用流程吊销的旧密钥不会因解禁自动恢复。
管理员可以直接为任意已启用用户签发设备密钥，目标用户无需先通过 EVE SSO 登录。

## 认证模式

- `off`：认证服务关闭，旧业务接口保持开放。
- `setup`：认证和管理接口受保护，现有业务接口暂不强制认证，供上线迁移使用。
- `enforce`：除 `/api/health`、管理员登录和 EVE SSO 起止接口外均要求认证。

密钥风控由 `EVE_SENTRY_SERVER_KEY_RISK_CONTROL` 独立控制，默认 `on`：
管理员也可以在 Web 的“系统管理 → 安全设置”中切换；保存后的 Web 设置优先于启动环境变量，
并在服务重启后保留。

- `on`：客户端上报 Listener 角色，服务端通过公共 ESI、允许军团和角色白名单持续校验。
- `off`：所有有效设备密钥直接可信；身份上报立即返回 `skipped=true`，不访问 ESI、
  不创建身份任务，也不会因角色判定吊销密钥。密钥本身的认证、吊销、账号禁用和权限范围
  仍然生效。

客户端允许设备密钥留空。留空时不会调用 `/api/v1/auth/me` 预检，不会扫描或上报
Listener 身份，也不会发送 `Authorization` 请求头。这只是客户端行为，不会绕过服务端
策略；`enforce` 模式仍会拒绝未认证的受保护请求。

HTTP 可以启用认证，但密码、Cookie 和 API 密钥会明文经过网络。可信内网可按实际环境
使用 HTTP，公网入口建议使用 HTTPS。

## 初始管理员

创建权限受限的密码文件：

```bash
sudo install -o eve-sentry -g eve-sentry -m 600 /dev/null /etc/eve-sentry/admin-password
sudo sh -c 'printf "%s" "请替换为至少12位随机密码" > /etc/eve-sentry/admin-password'
```

配置：

```dotenv
EVE_SENTRY_SERVER_AUTH_MODE=setup
EVE_SENTRY_SERVER_KEY_RISK_CONTROL=on
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_ADMIN=admin
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_PASSWORD_FILE=/etc/eve-sentry/admin-password
```

服务只在管理员不存在时创建账号，不会重复重置密码。创建完成后移除两个 Bootstrap
变量并删除密码文件。

## 桌面身份验证

以下流程仅在 `EVE_SENTRY_SERVER_KEY_RISK_CONTROL=on` 时执行。关闭风控时，管理员可在
用户管理中直接签发设备密钥，客户端无需 EVE SSO，Listener 上报也不会进入 ESI 校验队列。

1. 设备密钥是可选配置；用户可以从网页创建并填入，也可以保持为空。
2. 密钥为空时，客户端不调用 `/api/v1/auth/me`，不执行 Listener 身份扫描，也不发送
   `Authorization` 请求头，可直接尝试开启监控或预警；能否访问接口仍由服务端认证模式决定。
3. 已填写密钥时，客户端在开启监控或预警前通过受保护的账号接口验证；无效密钥会阻止开启。
4. 密钥有效后，用户可以独立开启 Listener 身份扫描；该开关默认关闭。开启后客户端使用已缓存的
   EVE Chatlogs 路径，只检查最近 24 小时修改过的 Local 日志；按账号分组后，每个账号只读取
   修改时间最新的一份。分组键使用中英文客户端日志文件名末尾的 `character_id`，最多处理
   64 个角色，不扫描全部历史日志。
   没有发现 `Listener` 不算校验失败，也不阻止启动。
5. 发现有效日志后，客户端调用 `/api/v1/client/identity-checks`，只提交文件名末尾的
   `character_id` 列表和客户端 ID。
6. 服务端持久化幂等任务并立即确认；后台 worker 直接按角色 ID 通过 ESI 获取规范角色名、
   军团和联盟，不再依赖名称搜索。
7. 角色属于任一允许军团，或角色 ID 位于该用户白名单时继续使用；否则触发风控。
8. 已配置密钥的验证长期有效；后续只有新增角色、规则变化或管理员操作会重新判定角色身份。

仅在已配置且验证通过设备密钥、并且用户开启 Listener 身份扫描时，客户端每 10 秒检查
Chatlogs 目录。目录没有新增文件时不重复枚举；新文件按修改时间筛选，并且每个账号只保留
当前最新的 Local 文件。尚未写出 `Listener` 的文件会在修改时间或大小变化后单独重读。
目录时间戳未变化时每 30 秒执行一次元数据兜底枚举；相同密钥和角色 ID 集合复用
同一个服务端任务，因此客户端超时、重试或重启都不会重复执行身份检查和写入成功审计。
带有效末尾 ID 的新格式日志只读取文件名和修改时间，不打开日志内容；没有末尾 ID 的旧日志
才读取文件头并通过角色名兼容接口校验。
新角色遇到 ESI 超时或无法解析时，服务端按退避策略静默重试，不停止已开启的监控或
预警，也不禁用用户。旧的 `/api/v1/client/identity-check` 同步接口仅用于滚动升级兼容。

确认任一角色既不属于允许军团、也不在该用户白名单时，服务端在同一事务内：

- 禁用整个用户。
- 吊销该用户全部会话和密钥。
- 保存角色、军团和判定原因到审计记录。
- 断开 SSE，并使客户端停止监控与预警。

管理员修改允许军团或角色白名单时，服务端根据已保存的验证角色立即重新计算授权。
如需恢复经过认证的客户端访问，管理员解禁后必须签发新密钥。客户端填写并验证新密钥
后会保留已处理文件记录，不重新扫描历史；开启 Listener 扫描并发现新角色后提交风控校验。
未配置密钥的客户端仍由
服务端认证模式决定能否访问。

## 服务密钥

QQ 机器人使用管理员为服务账号创建的 `service_readonly` 密钥：

```dotenv
EVE_SENTRY_EVENTS_URL=http://YOUR_SERVER/api/v1/events
EVE_SENTRY_API_KEY=eve_创建时显示的完整密钥
EVE_SENTRY_PUBLIC_URL=http://YOUR_SERVER
```

第三方程序接收 `bootstrap`、`monitoring_node`、`alert` 和 `safe` 事件的完整方式见
[预警消息 API 接入指南](alert-api.md)。

轮换时先创建新密钥并更新机器人，确认 SSE 已重连后再吊销旧密钥。

## 安全控制

- 网页会话 Cookie 为 `HttpOnly; SameSite=Strict`；HTTPS 请求额外设置 `Secure`。
- 网页非只读请求必须携带 `X-CSRF-Token`。
- 管理员登录同时按“IP + 用户名”和 IP 汇总失败次数限流，避免轮换用户名绕过。
- 普通用户不能访问 `/api/v1/admin/*`。
- 只读服务密钥不能写入数据，也不能读取其授权范围外的接口。
- SSE 每 30 秒检查一次主体状态，认证变更会主动唤醒检查。
