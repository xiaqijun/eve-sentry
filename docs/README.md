# EVE Sentry 文档索引

## 当前架构文档

- `docs/intel-platform-architecture.md`
  情报平台整体架构和当前实现状态，覆盖检测客户端、预警客户端、服务端、ESI、频道解析、分类告警、API 和存储规划。
- `docs/intel-workflows.md`
  第一版情报工作流，定义客户端只采集、服务端只查询未查询过 ESI 的角色、按 ESI 声望把中立/不良/糟糕统一归为敌对，以及去掉评分系统后的前后端行为。
- `docs/intel-platform-roadmap.md`
  当前完成度、第一版边界、待做清单和后续开发顺序。
- `docs/intel-config-api.md`
  服务端分类/告警配置 API、SQLite/JSON 启动方式和 runtime 配置注意事项。
- `docs/local-integration.md`
  本地联调启动顺序、健康检查、runtime data 和常见排查入口。
- `docs/monitor-client-packaging.md`
  Windows GPU 监控客户端的离线模型打包、压缩、分发和验收说明。
- `docs/server-deployment.md`
  Linux 服务端部署、systemd 模板、环境变量入口和客户端对接说明。

## 历史文档

- `docs/superpowers/specs/2026-06-24-eve-sentry-design.md`
- `docs/superpowers/plans/2026-06-24-eve-sentry-plan.md`

这两份是早期单机 OCR 方案文档，当前以 `intel-platform-*`、配置 API、联调和部署文档为准。
