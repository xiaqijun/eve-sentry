# EVE Sentry 文档

按角色选择入口：

## 客户端用户

- [客户端操作指南](client.md)：下载安装、连接服务端、选择 EVE 窗口、开始监控、开启预警和常见问题。
- [预警频道日志客户端](channel-client.md)：独立读取 EVE 预警频道 Chatlogs，并把原始消息交给服务端解析。

## 服务端运维

- [系统架构](architecture.md)：客户端、情报服务、Web 管理系统和数据来源之间的边界与数据流。
- [服务端部署](server-deployment.md)：安装服务端、配置 PostgreSQL、部署前端和反向代理、启动服务及上线验证。
- [CI/CD 与仓库边界](ci-cd.md)：五仓库职责、流水线触发条件、发布门禁、Secrets/Variables、验证和回滚。
- [认证与 EVE 身份校验](authentication.md)：认证模式、管理员初始化、设备密钥和 Listener 风控。
- [Web 管理系统](web-console.md)：页面职责、实时工作台、来袭分析和管理页面约束。
- [ESI 代理迁移计划](esi-proxy-migration-plan.md)：将公共 ESI 请求迁移到独立代理的架构、分阶段实施、安全和回滚方案。

## 集成与发布

- [预警消息 API 接入指南](alert-api.md)：第三方软件通过 SSE 或 JSON 获取实时预警和当前敌对星系。
- [完整 API 参考](api-reference.md)：服务端全部 HTTP 接口、参数和数据约定。
- [GitCode 镜像状态](gitcode-release-mirror.md)：客户端发布镜像的当前状态和限制。

规划文档不再作为项目文档维护；已完成的设计内容已合并到架构、Web 管理系统和 API 文档。

客户端用户不需要阅读服务端部署文档，也不需要配置 Python、OCR 模型或环境变量。
