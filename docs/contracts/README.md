# EVE Sentry 线协议

本目录是 EVE Sentry 多仓库联动的唯一协议来源，迁移自已废弃的
`eve-sentry-contracts` 仓库。服务端、客户端、QQ 机器人和 ESI Gateway
均以这里的 OpenAPI、JSON Schema、DTO、示例和兼容性测试为准。

## 目录

- `openapi/`：HTTP 接口定义
- `schemas/`：Bootstrap、SSE、ESI Gateway 和一次性 OCR 查询协议
- `dto/`：Python 与 TypeScript 数据传输对象
- `tests/fixtures/`：跨仓库兼容性样例
- `docs/`：协议边界、版本策略和开发流程

## 变更规则

- 所有仓库后续只在 `main` 分支开发。
- 协议新增优先使用可选字段，消费者必须忽略未知字段。
- 修改 HTTP、SSE、heartbeat command 或 OCR 查询字段时，先更新这里的协议，
  再分别修改消费者和生产者。
- `schema_version` 按 `<name>.v<major>` 演进；删除字段、改变类型或改变语义时
  必须升级 major 版本。

## 相关实现

- 服务端实现：`app/server/`、`app/esi/`
- 客户端实现：`eve-sentry-client`
- QQ 机器人实现：`eve-sentry-bot`
- 公共 ESI Gateway：`eve-sentry-esi-gateway`
