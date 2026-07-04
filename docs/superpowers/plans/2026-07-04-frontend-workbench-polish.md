# React 情报工作台前端打磨计划

> 执行方式：使用 sub-agent 按任务拆分实现，每个实现任务经过规格审查与代码质量审查。

## 目标

让 React 情报工作台的实时态和历史态更清楚：

- 星图节点只表达实时活跃情报，不再被历史报告或历史告警持续点亮。
- 中文界面文案保持可读，清理乱码和过短的单字指标。
- 敌对飞行员观察列表统一展示 OCR、预警频道、手动上报、zKill、ESI 等来源。
- 离线或过期后，实时情报从星图热度和观察列表中消失，历史情报仍可留档。

## 设计约束

- 不引入新的地图图库或设计系统。
- 不构造虚构情报样本验证业务效果；测试只使用受控契约数据。
- 后端历史 reports/alerts 继续保留，实时地图热度只来自 active intel。
- 前端构建仍使用 Vite + React + TypeScript + TanStack Query/Table。

## 任务清单

- [x] 修复服务端 snapshot 语义
  - `snapshot()` 先执行 active intel 过期清理。
  - 星图系统聚合改为使用 active intel。
  - 历史 reports/alerts/summary 继续保留。

- [x] 收敛星图节点实时信息
  - `buildTacticalGraph()` 不再用历史 alerts 单独点亮节点。
  - `hasAlerts`、`observationCount`、`threatLevel`、`threatScore` 跟随实时 map 计数。
  - 增加历史 alert 不应点亮节点的回归测试。

- [x] 清理星图文案与空态
  - 监控标记改为 `在线 N` / `离线 N`。
  - 节点指标改为 `实时目标`、`来源`、`击杀`。
  - 无实时目标时显示 `暂无实时敌对目标`。
  - 空态使用 overlay，不挤压星图 canvas。

- [x] 优化敌对飞行员观察列表
  - 来源标签统一为 `预警频道`、`本地OCR`、`手动上报`、`zKill`、`ESI`、`情报`。
  - 未知来源不直接泄露后端 source key。
  - 空名称 active intel 显示为 `未命名目标`。
  - 表格列为 `飞行员`、`星系`、`来源`、`威胁`、`最近出现`、`次数`。
  - 空态显示 `暂无实时敌对目标`。

- [x] 验证
  - 前端 focused tests 通过。
  - 前端生产构建通过。
  - 后端 active intel / store / HTTP 相关回归通过。
  - GitNexus detect_changes 已执行，整体风险为 CRITICAL，原因是后端 snapshot 影响 API 流程；已通过专项测试覆盖。

## 后续待做

- [ ] 进一步压缩星图节点指标宽度，避免密集区域标签重叠。
- [ ] 给 zKill/ESI 情报补独立详情面板，而不是塞进节点主标签。
- [ ] 视觉验收时检查真实 Tenal 地图在桌面端和窄屏下的节点可读性。
