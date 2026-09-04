# ESI Gateway 文档

Gateway 只提供无用户令牌的公共 ESI 代理能力。EVE SSO、OAuth token、contacts、
standings 和其他认证请求仍由 `eve-sentry` 服务端负责。

## 文档入口

- [缓存与存储设计](cache-and-storage.md)：PostgreSQL 持久层、Redis 热层、TTL、stale、
  刷新队列和 4C4G 资源边界。缓存策略的唯一权威来源。
- [运维手册](operations.md)：安装、环境变量、健康检查、CI/CD 和回滚步骤。

两份文档职责不同：缓存算法和数据语义只在“缓存与存储设计”维护；主机操作和发布步骤只在
“运维手册”维护，避免同一参数出现两份互相矛盾的副本。

公共路由变更时，先同步服务端的
[ESI 与缓存现状](https://github.com/xiaqijun/eve-sentry/blob/main/docs/server-deployment.md#公共-esi-gateway)，
再更新本仓库的接口和运维说明。后续默认直接在 `main` 开发并推送，由 Gateway 工作流自动
验证和部署。
