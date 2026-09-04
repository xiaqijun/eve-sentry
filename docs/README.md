# EVE Sentry 文档

本目录按“当前规范优先、操作手册分离、历史设计不重复维护”的原则维护。
接口、事件字段和兼容行为以 `api-reference.md` 为准；第三方接入步骤以
`alert-api.md` 为准；跨仓库变更先更新本目录和对应消费者文档。

按角色选择入口：

## 客户端用户

- [客户端操作指南](../client/docs/client.md)：下载安装、连接服务端、选择 EVE 窗口、开始监控、开启预警和常见问题。
- [预警频道日志客户端](channel-client.md)：独立读取 EVE 预警频道 Chatlogs，并把原始消息交给服务端解析。

## 服务端运维

- [系统架构](architecture.md)：客户端、情报服务、Web 管理系统和数据来源之间的边界与数据流。
- [服务端部署](server-deployment.md)：安装服务端、配置 PostgreSQL、部署前端和反向代理、启动服务及上线验证。
- [CI/CD 与仓库边界](ci-cd.md)：各仓库职责、`main` 推送触发、发布门禁、Secrets/Variables、验证和回滚。
- [认证与 EVE 身份校验](authentication.md)：认证模式、管理员初始化、设备密钥和 Listener 风控。
- [Web 管理系统](web-console.md)：页面职责、实时工作台、来袭分析和管理页面约束。
- [ESI 与缓存现状](server-deployment.md#公共-esi-gateway)：公共 ESI Gateway 的现行边界、缓存 TTL 和回退配置。

## 集成与发布

- [预警消息 API 接入指南](alert-api.md)：第三方软件通过 SSE 或 JSON 获取实时预警和当前敌对星系。
- [完整 API 参考](api-reference.md)：服务端全部 HTTP 接口、参数和数据约定。
- [单体仓库开发与联动](multi-repository-development.md)：目录职责、联调顺序、`main` 分支约定和发布门禁。
- [GitCode 镜像状态](gitcode-release-mirror.md)：客户端发布镜像的当前状态和限制。

独立迁移计划不再作为项目文档维护；已完成的设计内容合并到架构、部署和 API 文档，
避免同一配置在多个文件中出现不同版本。

## 组件文档

- [机器人查询与 SSE 约束](../bot/docs/README.md)
- [客户端按需 OCR 对接](../client/docs/on-demand-ocr-query.md)
- [ESI Gateway 缓存与运维](../esi-gateway/docs/README.md)

客户端用户不需要阅读服务端部署文档，也不需要配置 Python、OCR 模型或环境变量。
