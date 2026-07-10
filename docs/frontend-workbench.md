# React SPA 情报工作台设计轨道

> Current status (2026-07-09): zKillboard/killboard data is not available in the
> active product path. The workbench must not show fabricated killmail counts;
> node summaries should use real OCR/channel/ESI/server state only. Killmail
> fields remain deferred until a new bounded server-side design is approved.

> Current workflow baseline (2026-07-10): 前端不展示威胁评分。服务端返回
> `classification=red|white|neutral|unknown` 和 `reason` 作为兼容字段，业务展示映射为
> 敌对/友好/未知；星图节点和观察列表只展示敌对数量、分类原因和真实来源。

## 当前方向

`frontend/` 是独立维护的 React SPA，生产环境由 OpenResty/Nginx 托管静态资源，并把 `/api/` 反向代理到 Python intel server。Python intel server 不再托管旧内嵌 HTML 页面。

当前视觉目标不是通用后台 dashboard，而是 EVE 哨兵预警情报工作台：中心是可操作星图，右侧是敌对飞行员观察列表，底部和侧栏承载告警、态势、风险和系统状态。

本轮调整明确跳过生成星云 HUD 背景图。星图背景只使用 CSS 与 canvas 程序绘制，避免把生成图片纳入部署资产和验收链路。

## 已采用组件

- Vite + React 18 + TypeScript
- React Router
- TanStack Query
- Zustand
- `react-force-graph-2d`：战术星图、拖拽平移、滚轮缩放、节点选中、fitView
- `echarts` + `echarts-for-react`：仅用于后续趋势图；第一版不展示威胁评分和 ISK 风险仪表
- `@tanstack/react-table`：敌对飞行员观察列表
- Vitest + Testing Library

## 当前实现进度

- 已用 `react-force-graph-2d` 替换原 React Flow 星图交互。
- 星图节点显示星系名、安全等级、敌对数和最近 1 小时击毁数。
- 右侧 OCR 和情报列表已合并为“敌对飞行员观察列表”。
- 观察列表当前合并来源包括 reports 与 alerts；同一飞行员会按来源、分类、原因和最近时间合并为一条记录。
- 页面不会从 `raw_text` 猜测飞行员，不会构造假 zKill / ESI 情报填充界面。
- killboard 不进入第一版界面，不伪造击毁数据。
- 已补充前端测试覆盖战术图数据映射、核心工作台渲染、观察列表合并。

## 工作台信息架构

- 左侧：区域状态、导航、筛选、系统状态。
- 中间：战术星图，负责展示星系态势和当前选中上下文。
- 右侧：敌对飞行员观察列表，回答“现在有哪些人值得盯，以及为什么”。
- 下方：舰队动向、告警队列、最新分类事件。

核心原则：

- 星图负责态势，不承载长文本明细。
- 右侧列表负责对象，不再拆成 OCR 列表和情报列表。
- 选中星系后，右侧列表按该星系相关观察过滤。
- 不做 hover 气泡或自动弹出气泡，避免遮挡星图操作。
- 只展示真实接口、真实运行态或明确降级状态。

## 星图设计

星图由 `react-force-graph-2d` 承载，使用后端地图坐标作为固定坐标：

- 节点表示星系。
- 边表示星门连接。
- 鼠标拖拽平移。
- 滚轮缩放。
- Fit 按钮回到完整视野。
- 节点点击后联动右侧详情和观察列表。
- 告警路径和告警节点使用更高亮度，但不覆盖节点可点击区域。
- 背景由 CSS / canvas 绘制，不使用生成图片。

节点常驻摘要格式：

```text
乌寞-F4
敌 2  损 1
```

字段含义：

- `敌`：该星系当前敌对活跃观察数量；中立声望、不良声望、糟糕声望统一归入敌对。
- `损`：该星系最近 1 小时击毁数量；没有真实击毁数据时不显示虚构数值。

## 敌对飞行员观察列表

右侧主面板统一为“敌对飞行员观察列表”，合并以下真实来源：

- OCR 本地识别
- 情报频道解析
- 服务端预警 alerts
- ESI standing / contacts / 军团联盟
- 手动情报

同一个飞行员被多个来源命中时合并为一条观察记录，避免重复刷屏。列表建议字段：

```text
飞行员          星系      来源            分类    原因              最近出现
Varg Vikernes  乌寞-F4   OCR / 预警      敌对    hostile_alliance  20:46
Khanid Shadows 乌寞-F4   频道 / ESI      友好    friendly_standing 20:45
```

字段约定：

- 飞行员名称。
- 关联星系。
- 军团 / 联盟，只在真实数据存在时展示。
- 来源标签按真实来源组合展示。
- 分类: 敌对、友好或未知。
- 命中原因: 例如 `hostile_alliance`、`friendly_corporation`、`hostile_standing`。
- 最近出现时间。

交互约定：

- 点击星系后，观察列表过滤到该星系相关飞行员。
- 点击飞行员后，星图高亮关联星系。
- 右侧详情展示完整证据链，星图节点只保留摘要。

## 开发与部署

本地开发：

```bash
cd frontend
npm install
npm run dev
```

生产构建：

```bash
cd frontend
npm run build
```

生产入口约定：

- `/` 指向 `frontend/dist/`
- `/api/` 反向代理到 Python intel server
- Python intel server 的直连根路径不再服务页面，只保留 JSON API/SSE。
- 工作台和预警客户端默认订阅 `/api/v1/events` SSE；旧 `/api/events` 仅保留兼容。
- OpenResty/Nginx 模板必须对 `/api/v1/events` 和 `/api/events` 关闭 buffering，避免告警推送被代理缓存。

## 测试重点

- 星图节点数据来自真实接口契约或受控测试 fixture，不使用虚构情报样本做产品验证。
- 节点点击能正确更新选中星系。
- 平移、缩放、Fit 后仍可稳定选中节点。
- SSE 告警能合并进入缓存，不整页刷新。
- 手动上报与配置表单提交 payload 正确。
- 观察列表按真实来源合并飞行员。
- ESI 不可用时展示明确降级状态；killboard 不进入第一版状态展示。

## 下一步

- 继续联调真实 `/api/v1/bootstrap` 数据，确认星图节点和观察列表字段完整。
- 把 `classification` / `reason` 接入节点摘要、观察列表和告警详情。
- 优化打包体积：ECharts 按需导入，星图模块按路由切分。
- 清理旧 React Flow 依赖和历史节点组件，前提是 `summarizeWorkbench` 等剩余逻辑完成拆分。
