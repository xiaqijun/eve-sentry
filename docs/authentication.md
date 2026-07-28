# 认证与 EVE 身份校验

## 登录与密钥

| 身份 | 登录方式 | 权限 |
| --- | --- | --- |
| 管理员 | 用户名和密码 | 全部管理页面、账号安全和业务接口 |
| 普通用户 | EVE SSO | 态势页、报表和自己的设备密钥 |
| 桌面设备密钥 | `Authorization: Bearer <key>` | 有效密钥可访问客户端 API；Listener 用于持续风控 |
| 只读服务密钥 | `Authorization: Bearer <key>` | 仅 `GET /api/v1/bootstrap` 和 `GET /api/v1/events` |

普通用户只有在 EVE SSO 角色属于管理员配置的允许军团时才能登录。管理员账号不使用
EVE SSO。EVE SSO 登录和态势页 ESI 授权共用一个应用和回调：

```text
/api/v1/auth/esi/callback
```

密码使用 Argon2id。会话和 API 密钥只保存哈希，完整密钥只在创建时显示一次。设备
密钥可吊销、重新启用或删除记录；已被用户禁用流程吊销的旧密钥不会因解禁自动恢复。

## 认证模式

- `off`：认证服务关闭，旧业务接口保持开放。
- `setup`：认证和管理接口受保护，现有业务接口暂不强制认证，供上线迁移使用。
- `enforce`：除 `/api/health`、管理员登录和 EVE SSO 起止接口外均要求认证。

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
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_ADMIN=admin
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_PASSWORD_FILE=/etc/eve-sentry/admin-password
```

服务只在管理员不存在时创建账号，不会重复重置密码。创建完成后移除两个 Bootstrap
变量并删除密码文件。

## 桌面身份验证

1. 用户创建桌面设备密钥。
2. 客户端在开启监控或预警前通过受保护的账号接口验证密钥；有效才允许开启。
3. Listener 检测是独立后台任务。客户端自动定位 EVE Chatlogs，并扫描全部历史日志。
   没有发现 `Listener` 不算校验失败，也不阻止启动。
4. 发现 `Listener` 后，客户端调用 `/api/v1/client/identity-check` 提交角色名。
5. 服务端通过 ESI 解析角色 ID、军团和联盟。
6. 角色属于任一允许军团，或角色 ID 位于该用户白名单时继续使用；否则触发风控。
7. 密钥验证长期有效；后续只有新增角色、规则变化或管理员操作会重新判定角色身份。

客户端每 10 秒重新确认当前活跃的 Chatlogs 目录，并只查找新增日志文件，不重复扫描
已处理文件的内容。新文件尚未写出 `Listener` 时不会标记完成。新角色遇到 ESI 超时或
无法解析时，后台静默重试，不停止已开启的监控或预警，也不禁用用户。

确认任一角色既不属于允许军团、也不在该用户白名单时，服务端在同一事务内：

- 禁用整个用户。
- 吊销该用户全部会话和密钥。
- 保存角色、军团和判定原因到审计记录。
- 断开 SSE，并使客户端停止监控与预警。

管理员修改允许军团或角色白名单时，服务端根据已保存的验证角色立即重新计算授权。
管理员解禁后必须签发新密钥。新密钥有效即可启动；客户端仍会重新扫描历史日志，并在
发现 `Listener` 后提交风控校验。

## 服务密钥

QQ 机器人使用管理员为服务账号创建的 `service_readonly` 密钥：

```dotenv
EVE_SENTRY_EVENTS_URL=http://YOUR_SERVER/api/v1/events
EVE_SENTRY_API_KEY=eve_创建时显示的完整密钥
EVE_SENTRY_PUBLIC_URL=http://YOUR_SERVER
```

轮换时先创建新密钥并更新机器人，确认 SSE 已重连后再吊销旧密钥。

## 安全控制

- 网页会话 Cookie 为 `HttpOnly; SameSite=Strict`；HTTPS 请求额外设置 `Secure`。
- 网页非只读请求必须携带 `X-CSRF-Token`。
- 登录接口有失败限流。
- 普通用户不能访问 `/api/v1/admin/*`。
- 只读服务密钥不能写入数据，也不能读取其授权范围外的接口。
- SSE 每 30 秒检查一次主体状态，认证变更会主动唤醒检查。
