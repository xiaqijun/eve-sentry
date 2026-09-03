# 最小初始化计划

本轮只冻结边界和可执行样例，不修改四个实现仓库，也不触发部署。

## 阶段 0：已完成（契约草案）

- [x] 记录四仓库边界、SemVer 和 `schema_version` 规则。
- [x] 冻结 `/api/v1/bootstrap`、`/api/v1/events`、上报接口和敌对星系轮询的最小 OpenAPI。
- [x] 冻结 `intel_bootstrap.v1`、`alert`、`safe`、`monitoring_node` 的必需字段。
- [x] 定义 `hostile_movement_event.v1`，作为新增且可选的 SSE 事件；旧消费者继续依赖
      bootstrap/`monitoring_node` 校准。
- [x] 记录私有 ESI Gateway 的路由、认证、缓存命中和错误语义。
- [x] 提供无依赖 Python DTO、TypeScript DTO、共享 JSON fixture 和 pytest 骨架。
- [x] 冻结一次性 OCR 查询协议：四类查询、幂等 `request_id`、任务 `expires_at`、
      heartbeat `capture_ocr_once`、多窗口结果和部分节点失败语义。

## 阶段 1：实现接入（后续）

1. 在服务端生成 OpenAPI 与 JSON Schema 的 contract test 报告；先验证现有响应，再考虑
   发出 `hostile_movement`。
2. 在客户端切换到固定版本的 Python/TypeScript DTO，不引入服务端相对路径。
3. 在机器人增加事件名白名单、`hostile_movement` 处理和 bootstrap 回放测试；未知事件
   只记录指标，不阻断 SSE。
4. 在 ESI Gateway CI 中锁定 `/health` 和五类 allow-list 路由，验证 401/403/404/502。

## 阶段 2：兼容性门禁（后续）

- 每次契约发布运行 fixture round-trip、OpenAPI 结构检查和跨语言快照比较。
- 对服务端和机器人分别运行 GitNexus `detect_changes`，确认只影响预期流程。
- MAJOR 版本需要迁移说明、双写/双读窗口和消费者确认；禁止静默改变字段含义。

## 待确认事项

- `hostile_movement` 是否由服务端直接发出，还是先由机器人从 bootstrap 推导。
- 是否需要把 `source_instance`、`client_id` 纳入移动事件的稳定身份键。
- ESI Gateway 是否在独立仓库发布自己的 SemVer，还是跟随本仓库版本。
