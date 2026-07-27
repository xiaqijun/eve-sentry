# 用户认证与 EVE 身份校验

## 权限模型

- `admin`：管理用户、密码、状态、允许军团、用户角色白名单、密钥和审计记录。
- `member`：修改自己的密码，创建和吊销自己的桌面客户端密钥。
- `desktop` 密钥：首次完成 EVE 日志身份校验后可长期使用服务端 API。
- `service_readonly` 密钥：仅允许读取 `/api/v1/bootstrap` 和 `/api/v1/events`，用于 QQ 机器人。

密码使用 Argon2id。网页登录会话和 API 密钥只在数据库保存 SHA-256 哈希；完整密钥仅在创建时显示一次。解禁用户不会恢复旧密钥，必须重新签发桌面密钥并重新完成首次身份校验。

## 创建初始管理员

先创建只允许服务用户读取的密码文件：

```bash
sudo install -o eve-sentry -g eve-sentry -m 600 /dev/null /etc/eve-sentry/admin-password
sudo sh -c 'printf "%s" "请替换为至少12位随机密码" > /etc/eve-sentry/admin-password'
```

在 `/etc/eve-sentry/eve-sentry.env` 中配置：

```dotenv
EVE_SENTRY_SERVER_AUTH_MODE=setup
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_ADMIN=admin
EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_PASSWORD_FILE=/etc/eve-sentry/admin-password
```

服务启动时只在管理员不存在时创建账号，不会使用文件内容反复重置现有密码。首次成功创建后可从环境文件移除两个 Bootstrap 变量，并安全删除密码文件。

## 桌面客户端校验

1. 管理员先配置一个或多个允许军团 ID，或给具体服务用户添加角色 ID 白名单。
2. 用户在账号页创建桌面密钥，完整密钥只显示一次。
3. 客户端使用 Windows DPAPI 保存密钥和本地日志索引。
4. 新密钥或本地索引缺失时，客户端扫描 EVE Chatlogs 中全部历史文件的 `Listener`。
5. 首次成功后认证永久有效。每 10 秒只枚举新增日志文件，并提交新增角色。
6. 新文件尚未写出完整 `Listener` 时保持待处理；ESI 超时或无法确认时暂停监控、预警和上报并重试。
7. 服务端确认任一角色既不属于允许军团、也不在该用户白名单时，会在同一事务禁用用户并吊销全部会话和密钥。

管理员移除允许军团或角色白名单时，服务端会立即根据已保存的验证角色重新计算权限，不依赖客户端续签。

## QQ 机器人密钥

在管理员页选中机器人所属服务用户，创建“只读服务密钥”，然后配置机器人：

```dotenv
EVE_SENTRY_EVENTS_URL=https://sentry.example.com/api/v1/events
EVE_SENTRY_API_KEY=eve_创建时显示的完整密钥
EVE_SENTRY_PUBLIC_URL=https://sentry.example.com
```

该密钥不能读取用户、报表或其他 API，也不能写入数据。轮换时先创建新密钥并更新机器人，确认 SSE 已重连后再吊销旧密钥。

## 安全上线顺序

1. 备份 PostgreSQL 或 SQLite 数据库。
2. 升级代码并设置 `EVE_SENTRY_SERVER_AUTH_MODE=setup`，创建认证表和管理员；此时认证管理端点受保护，现有数据、OCR 和 SSE 接口暂不强制认证。
3. 创建初始管理员，配置允许军团和必要的用户角色白名单。
4. 为域名配置可信 TLS 证书，验证 HTTP 跳转、HTTPS 和 HSTS。
5. 创建 QQ 机器人只读服务密钥，升级机器人并确认 Bootstrap/SSE 正常。
6. 升级桌面客户端，为用户创建账号并完成首次历史日志校验。
7. 将 `EVE_SENTRY_SERVER_AUTH_MODE=enforce`，重启服务并验证健康检查、网页登录、OCR、心跳和 SSE。

不要在远程明文 HTTP 地址上发送密钥。客户端会拒绝这种配置；只有本机 `localhost`/`127.0.0.1` 调试允许 HTTP。
