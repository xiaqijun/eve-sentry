# ESI 代理迁移计划

## 目标与边界

将高频、无用户令牌的公共 ESI 请求迁移到 `47.243.104.165`，由该机器统一完成 ESI 出站访问、缓存和限流；`114.132.167.239` 上的情报服务通过内网调用代理。

第一阶段只迁移公共数据：角色、军团、联盟、星系资料和 `/universe/ids`、`/universe/names` 批量解析。EVE SSO 登录、OAuth token 刷新、角色当前位置和联系人 standings 暂时保留在 114，不把用户 token 交给代理。

## 现状与依据

- 47 直连 ESI 的 Python `urllib` 中位数约 `0.60s`，10 次样本较稳定。
- 114 直连 ESI 中位数约 `0.76~0.83s`，出现过 `2.67s` 峰值。
- 114 到 47 的网络往返约 `14ms`，代理转发后的理论常态耗时约 `0.61~0.65s`。
- `EsiClient` 的 GitNexus 影响为 LOW：直接上游 2 个、总影响 5 个。
- `EsiResolver` 的 GitNexus 影响为 LOW：直接上游 1 个。
- `EveSsoClient` 的 GitNexus 影响为 MEDIUM：直接上游 6 个、总影响 16 个；认证流不随公共代理一起迁移。

## 目标架构

```text
114 情报服务
  -> ESI 后端接口（配置选择 remote/local）
      -> 47 ESI Gateway（内网鉴权、缓存、并发合并、限流）
          -> esi.evetech.net

114 认证/SSO 流程
  -> 本地 EsiAuthenticatedSession / EveSsoClient
  -> EVE SSO 和带 token 的 ESI 接口
```

代理不可用时，114 自动回退到本地公共 ESI，不能阻塞 OCR 上报、心跳或告警生成。

## 阶段 0：冻结契约与基线

1. 在 `app/esi/client.py` 抽象公共 ESI 后端接口，保持现有 `resolve_ids`、`resolve_names`、`get_character`、`get_character_affiliations`、`get_corporation`、`get_alliance`、`get_system` 方法签名。
2. 为每个请求记录结构化耗时：后端、端点、状态、缓存命中、总耗时；禁止记录 Authorization、token 和完整请求体。
3. 增加基线测试：本地客户端请求、JSON 错误、HTTP 错误、超时、空结果和批量解析。
4. 在 114 记录一周公共 ESI 请求的数量、P50/P95/P99、错误率和缓存命中率，作为迁移对照。

验收：现有 ESI 测试全部通过，默认行为不变，日志中没有敏感信息。

## 阶段 1：在 47 部署 ESI Gateway

### 服务接口

建议独立 Python 服务，监听 ZeroTier `10.233.53.17:8787`，仅允许 114 的 `10.233.53.204` 访问。接口只提供白名单操作，不提供通用 URL 转发：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 存活、版本和上游状态摘要 |
| `POST` | `/v1/universe/ids` | 名称批量解析 |
| `POST` | `/v1/universe/names` | ID 批量解析 |
| `GET` | `/v1/characters/{id}` | 角色资料 |
| `POST` | `/v1/characters/affiliation` | 批量角色当前军团、联盟和派系归属（最多 1000 个 ID） |
| `GET` | `/v1/corporations/{id}` | 军团资料 |
| `GET` | `/v1/alliances/{id}` | 联盟资料 |
| `GET` | `/v1/systems/{id}` | 星系资料 |

响应统一包含 `data`、`cache`、`fetched_at` 和可机器读取的错误码。代理内部继续使用 `EsiClient`，不复制业务层的 `EsiResolver` 逻辑。

### 安全与资源限制

- 只允许 `114.132.167.239` 来源访问；健康检查可限制为本机或管理网段。
- 使用独立的长随机服务密钥，放在 47 的 root-only 配置文件；请求使用 `Authorization: Bearer` 或 HMAC。
- 设置单请求超时 10 秒、请求体上限 64 KiB、并发上限和每来源速率限制。
- 只允许固定 ESI 路径和正整数 ID；禁止代理任意外部 URL。
- ESI 正常结果缓存 12~24 小时；未找到结果短缓存 10 分钟；失败不写入长缓存。
- 对相同 cache key 合并并发请求，避免 OCR 批量上报造成请求风暴。
- 代理日志只保留端点、状态、耗时、缓存状态和 request id。

### 部署

1. 47 安装独立虚拟环境和 systemd 单元 `eve-sentry-esi-gateway.service`。
2. 缓存写入 `/var/lib/eve-sentry-esi/`，配置写入 `/etc/eve-sentry-esi/gateway.env`。
3. 只绑定内网可达地址；如果两台机器不在同一私网，使用 WireGuard/Tailscale，而不是把 8787 暴露到公网。
4. 配置 systemd `Restart=always`、资源限制和健康检查。
5. 从 114 执行 100 次健康和真实解析请求，确认 P50/P95、错误率和断开重试行为。

验收：Gateway 连续运行 24 小时；健康检查可用；114 白名单地址之外无法调用；缓存命中和限流指标可见。

## 阶段 2：切换 114 公共解析流

1. 新增配置：

```dotenv
EVE_SENTRY_ESI_BACKEND=remote
EVE_SENTRY_ESI_GATEWAY_URL=http://10.x.x.x:8787
EVE_SENTRY_ESI_GATEWAY_TOKEN=...
EVE_SENTRY_ESI_REMOTE_TIMEOUT=8
EVE_SENTRY_ESI_LOCAL_FALLBACK=1
```

2. 实现 `RemoteEsiClient`，让它兼容 `EsiClient` 的公共方法；不要修改 `EsiResolver`、`IntelStore`、地图配置和身份分类的数据结构。
3. 远端请求失败时仅对当前调用回退本地 ESI，带指数退避和短暂熔断；缓存仍由 `EsiResolver` 的现有逻辑负责业务 TTL，Gateway 负责网络级缓存。
4. 灰度切换：先只启用地图刷新或后台身份补全，再切换全部公共解析；观察 24~48 小时后扩大范围。
5. 对比迁移前后 P50/P95/P99、ESI 错误率、OCR 入站延迟、身份解析完成时间和告警产生延迟。

验收：公共解析结果与本地实现一致；代理故障时 OCR、心跳和告警仍可用；P95 不高于直连基线；没有 token 泄漏。

## 阶段 3（待做）：Gateway 支持认证 ESI 请求

当前认证 ESI 仍由 114 服务端直连，Gateway 不接触用户 OAuth token。本阶段登记为后续工作，
不得与公共 ESI 代理改造混合上线。

待完成事项：

- 设计 Gateway 认证请求协议和服务间身份校验，禁止通用 URL 转发。
- 明确 access token / refresh token 的存储、传输、轮换、撤销和审计边界。
- 为 standings、contacts、location 等私有接口建立按授权角色隔离的缓存键，禁止跨账号复用。
- 增加 token 脱敏、内存生命周期控制、限流、超时、失败回退和监控指标。
- 完成安全评审、密钥轮换演练、故障回滚和真实 ESI 集成测试后再实施。

在该阶段完成前，继续采用下面的现行方案：

不在第一阶段迁移 `EveSsoClient`、`EsiAuthenticatedSession` 和 `EsiLoginManager`。如确实需要统一出口，另立设计：

- 方案 A：SSO 和 token 继续留在 114，仅把无 token 的公共资料走 Gateway，推荐。
- 方案 B：47 承担 OAuth 回调和 token 存储，需要重新审计 Cookie、CSRF、加密存储、备份和访问边界，风险为 MEDIUM/HIGH。
- 方案 C：114 保留 token，只由 47 代发带 token 请求；不推荐，代理日志、内存和调试链路都可能接触 bearer token。

只有在明确的安全评审、密钥轮换方案和回滚演练完成后，才考虑方案 B 或 C。

## 回滚方案

1. 将 `EVE_SENTRY_ESI_BACKEND=local`，重启 114 情报服务即可恢复直连。
2. 保留 Gateway 运行但停止流量，便于保留诊断日志；确认稳定后再停服务。
3. 回滚不删除 114 本地 `esi_cache.json`，避免缓存丢失导致瞬时请求洪峰。
4. 若代理返回错误数据，立即禁用远端后端并清理 Gateway 对应缓存命名空间。

## 测试与上线清单

- 单元：RemoteEsiClient 协议、认证、超时、重试、熔断、回退和响应校验。
- 集成：47 Gateway 与真实 ESI、114 到 47 网络、systemd 重启、服务密钥错误和来源限制。
- 回归：`tests/test_esi_client.py`、`tests/test_esi_resolver.py`、`tests/test_intel_store.py`、`tests/test_auth.py`、地图刷新和身份 worker 测试。
- 现场：连续 OCR 上报、批量角色解析、Esi health、PostgreSQL 身份任务、SSE 告警延迟。
- 发布前运行 Python/前端全量测试、生产构建和 GitNexus `detect_changes(scope="all")`。

## 预计节奏

按一个人维护、两台机器均可 SSH 管理估算：

| 阶段 | 产出 | 预计时间 |
| --- | --- | ---: |
| 0 | 接口抽象、指标和测试 | 0.5~1 天 |
| 1 | Gateway、systemd、安全限制和部署 | 1~2 天 |
| 2 | RemoteEsiClient、灰度、回退和监控 | 1~2 天 |
| 3 | 认证 ESI 评估（可选） | 1 天设计，实施另估 |

总体建议：先完成阶段 0~2，稳定运行一周后再决定是否需要阶段 3。
